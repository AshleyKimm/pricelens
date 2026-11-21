import json
import sqlite3
from datetime import date

import pytest
from helpers import line, receipt

from pricelens import db
from pricelens.categories import CATEGORIES, ItemLabel, description_key, review_reasons
from pricelens.categorize import FakeCategorizer, ItemToLabel, parse_and_check
from pricelens.errors import AuthError, MalformedOutputError, TransientAPIError
from pricelens.labeling import categorize_all, categorize_receipt, fix_label, list_labels
from pricelens.verify import verify

TODAY = date(2026, 11, 21)
ANSWERS = {"MILK 1GAL": ("milk", "dairy_eggs", "high"), "BNNA ORG": ("bananas", "produce", "high"),
           "TP MINT": ("toothpaste", "personal_care", "medium")}


def add(conn, lines, store="TEST MART", source="public", sha=None, day="11/18/2026"):
    total = sum(int(round(float(l.line_total) * 100)) for l in lines)
    amt = f"{total // 100}.{total % 100:02d}"
    ext = receipt(lines, subtotal=amt, total=amt, store=store, date_raw=day,
                  date_iso=f"2026-{day[:2]}-{day[3:5]}")
    res = verify(ext, TODAY)
    return db.save_receipt(conn, sha256=sha or f"sha-{store}-{day}-{len(lines)}-{source}", redacted_path=None,
                           result=res, model="fake", prompt_version="fake", extraction_json="{}", source=source)


@pytest.fixture
def conn():
    return db.connect(":memory:")


MILK = line("MILK 1GAL 3.49", "3.49", desc="MILK 1GAL")
BNNA = line("BNNA ORG 0.78", "0.78", desc="BNNA ORG")


def test_category_list_matches_database_constraint(conn):
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'item_labels'").fetchone()[0]
    for c in CATEGORIES:
        assert f"'{c}'" in sql


def test_labels_are_stored_and_cached(conn):
    rid = add(conn, [MILK, BNNA])
    cat = FakeCategorizer(ANSWERS)
    s = categorize_receipt(conn, rid, cat)
    assert (s.new_labels, s.cached, s.flagged) == (2, 0, 0)
    rid2 = add(conn, [MILK], day="11/19/2026")  # same store, same printed name, different price/day
    s2 = categorize_receipt(conn, rid2, cat)
    assert (s2.new_labels, s2.cached) == (0, 1)
    assert len(cat.calls) == 1  # second receipt made zero calls


def test_cache_is_per_store(conn):
    cat = FakeCategorizer(ANSWERS)
    categorize_receipt(conn, add(conn, [MILK], store="A MART"), cat)
    categorize_receipt(conn, add(conn, [MILK], store="B MART"), cat)
    assert len(cat.calls) == 2


def test_duplicate_items_in_one_receipt_sent_once(conn):
    cat = FakeCategorizer(ANSWERS)
    categorize_receipt(conn, add(conn, [MILK, MILK]), cat)
    assert len(cat.calls[0]) == 1


def test_cache_key_ignores_price_and_spacing():
    assert description_key("milk  1gal ") == description_key("MILK 1GAL") == "MILK 1GAL"


def test_review_flags():
    def L(name, cat, conf):
        return ItemLabel(id=1, normalized_name=name, variety="", pack_quantity=None, pack_unit=None, category=cat, confidence=conf)
    assert review_reasons(L("milk", "dairy_eggs", "high")) == []
    assert any(r.startswith("C2") for r in review_reasons(L("toothpaste", "personal_care", "medium")))
    assert any(r.startswith("C1") for r in review_reasons(L("", "uncertain", "low")))
    assert any(r.startswith("N2") for r in review_reasons(L("milk 1 gal", "dairy_eggs", "high")))
    assert any(r.startswith("N1") for r in review_reasons(L("  ", "dairy_eggs", "high")))
    assert any(r.startswith("N3") for r in review_reasons(L("x" * 41, "other", "high")))


def test_flagged_labels_are_stored_with_reasons(conn):
    rid = add(conn, [line("TP MINT 3.49", "3.49", desc="TP MINT"), line("ITM 40017 GV 1.00", "1.00", desc="ITM 40017 GV")])
    s = categorize_receipt(conn, rid, FakeCategorizer(ANSWERS))
    assert s.flagged == 2
    rows = {r["description_key"]: r for r in list_labels(conn, needs_review_only=True)}
    assert rows["ITM 40017 GV"]["category"] == "uncertain" and rows["ITM 40017 GV"]["normalized_name"] is None
    assert "C2" in rows["TP MINT"]["review_reasons"]


def test_ids_must_match_exactly():
    items = [ItemToLabel(1, "A", "A"), ItemToLabel(2, "B", "B")]
    one = lambda i, n="x": {"id": i, "normalized_name": n, "variety": "", "pack_quantity": None, "pack_unit": None, "category": "other", "confidence": "high"}
    assert len(parse_and_check(json.dumps({"items": [one(1), one(2)]}), [1, 2]).items) == 2
    for bad in ([one(1)], [one(1), one(2), one(3)], [one(1), one(1), one(2)]):
        with pytest.raises(MalformedOutputError):
            parse_and_check(json.dumps({"items": bad}), [i.id for i in items])


def test_invented_category_is_rejected():
    with pytest.raises(MalformedOutputError):
        parse_and_check(json.dumps({"items": [{"id": 1, "normalized_name": "x", "variety": "", "pack_quantity": None, "pack_unit": None, "category": "groceries",
                                               "confidence": "high"}]}), [1])


def test_bad_batch_stores_nothing(conn):
    rid = add(conn, [MILK, BNNA])
    drop_one = lambda items: json.dumps({"items": [{"id": 1, "normalized_name": "milk", "variety": "", "pack_quantity": None, "pack_unit": None, "category": "dairy_eggs",
                                                    "confidence": "high"}]})
    with pytest.raises(MalformedOutputError):
        categorize_receipt(conn, rid, FakeCategorizer(respond=drop_one))
    assert list_labels(conn) == []


def test_private_receipts_never_reach_a_training_tier(conn):
    rid = add(conn, [MILK], source="private")
    cat = FakeCategorizer(ANSWERS, trains_on_input=True)
    s = categorize_receipt(conn, rid, cat)
    assert s.skipped and "private" in s.skipped and cat.calls == []
    # a no-training provider may label it
    assert categorize_receipt(conn, rid, FakeCategorizer(ANSWERS)).new_labels == 1


def test_rejected_receipts_are_skipped(conn):
    rid = add(conn, [MILK])
    db.set_status(conn, rid, "rejected")
    assert categorize_receipt(conn, rid, FakeCategorizer(ANSWERS)).skipped == "receipt rejected"


def test_one_failure_does_not_stop_the_rest(conn):
    a, b = add(conn, [MILK], store="A MART"), add(conn, [BNNA], store="B MART")

    class Flaky(FakeCategorizer):
        def label(self, store, items, known_names=()):
            if store == "A MART":
                raise TransientAPIError("503")
            return super().label(store, items, known_names)

    out = categorize_all(conn, Flaky(ANSWERS))
    assert out[0].error and "TransientAPIError" in out[0].error
    assert out[1].new_labels == 1


def test_auth_failure_stops_everything(conn):
    add(conn, [MILK], store="A MART"), add(conn, [BNNA], store="B MART")
    with pytest.raises(AuthError):
        categorize_all(conn, FakeCategorizer(AuthError("bad key")))


def test_human_fix_overrides_ai_and_is_never_overwritten(conn, tmp_path):
    rid = add(conn, [line("TP MINT 3.49", "3.49", desc="TP MINT")])
    categorize_receipt(conn, rid, FakeCategorizer({"TP MINT": ("toilet paper", "household", "high")}))
    path = fix_label(conn, "test mart", "tp mint", "toothpaste", "personal_care", root=tmp_path)
    row = list_labels(conn)[0]
    assert (row["normalized_name"], row["category"], row["source"], row["needs_review"]) == \
        ("toothpaste", "personal_care", "human", 0)
    # re-categorizing never touches the human row (it's a cache hit)
    cat = FakeCategorizer({"TP MINT": ("toilet paper", "household", "high")})
    categorize_receipt(conn, add(conn, [line("TP MINT 3.49", "3.49", desc="TP MINT")], day="11/19/2026"), cat)
    assert cat.calls == [] and list_labels(conn)[0]["normalized_name"] == "toothpaste"
    # saved as a regression case, with what the AI said before
    case = json.loads(path.read_text().splitlines()[0])
    assert path.parent.name == "synthetic"
    assert case["expected_name"] == "toothpaste" and case["ai_previous"]["normalized_name"] == "toilet paper"


def test_private_corrections_stay_out_of_git(conn, tmp_path):
    add(conn, [MILK], source="private")
    path = fix_label(conn, "TEST MART", "MILK 1GAL", "milk", "dairy_eggs", root=tmp_path)
    assert path.parent.name == "private"


def test_human_input_is_validated_too(conn, tmp_path):
    add(conn, [MILK])
    with pytest.raises(ValueError):
        fix_label(conn, "TEST MART", "MILK 1GAL", "milk 1 gal", "dairy_eggs", root=tmp_path)  # digits
    with pytest.raises(ValueError):
        fix_label(conn, "TEST MART", "MILK 1GAL", "milk", "groceries", root=tmp_path)
    with pytest.raises(ValueError):
        fix_label(conn, "TEST MART", "NOT ON ANY RECEIPT", "milk", "dairy_eggs", root=tmp_path)
    with pytest.raises(ValueError):
        fix_label(conn, "NO SUCH STORE", "MILK 1GAL", "milk", "dairy_eggs", root=tmp_path)


def test_migration_upgrades_a_layer1_database(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript((db.MIGRATIONS / "001_layer1.sql").read_text())  # a Layer 1 database, user_version 0
    old.execute("INSERT INTO stores(name, name_key) VALUES ('X', 'x')")
    old.execute("""INSERT INTO receipts(image_sha256, store_id, status, model, prompt_version, extraction_json)
                   VALUES ('s', 1, 'verified', 'm', 'p', '{}')""")
    old.execute("""INSERT INTO line_items(receipt_id, line_no, raw_text, description, kind, section, line_total_cents)
                   VALUES (1, 1, 'MILK  1GAL 3.49', 'milk  1gal', 'item', 'items', 349)""")
    old.commit()
    old.close()
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert conn.execute("SELECT description_key FROM line_items").fetchone()[0] == "MILK 1GAL"
    assert conn.execute("SELECT source FROM receipts").fetchone()[0] == "private"  # unknown origin -> safe default
    db.connect(path)  # running again is a no-op


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "x.db"
    db.connect(path).close()
    bad = tmp_path / "migrations"
    bad.mkdir()
    for f in db.MIGRATIONS.glob("*.sql"):
        (bad / f.name).write_text(f.read_text())
    (bad / "999_broken.sql").write_text("CREATE TABLE ok_table(x); THIS IS NOT SQL;")
    monkeypatch.setattr(db, "MIGRATIONS", bad)
    with pytest.raises(sqlite3.Error):
        db.connect(path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3  # the last good migration
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'ok_table'").fetchone()[0] == 0


def test_all_receipts_of_a_store_share_one_request(conn):
    cat = FakeCategorizer(ANSWERS)
    add(conn, [MILK], day="11/18/2026"), add(conn, [BNNA], day="11/19/2026"), add(conn, [MILK], store="B MART")
    out = categorize_all(conn, cat)
    assert len(cat.calls) == 2  # one per store, not one per receipt
    assert sorted(len(c) for c in cat.calls) == [1, 2]
    assert sum(s.calls for s in out) == 2


def test_cross_store_search_uses_normalized_names(conn):
    from pricelens.prices import latest_by_store, price_changes
    add(conn, [line("BNNA ORG 0.59", "0.59", desc="BNNA ORG")], store="A MART", day="11/01/2026")
    add(conn, [line("BNNA ORG 0.69", "0.69", desc="BNNA ORG")], store="A MART", day="11/08/2026")
    add(conn, [line("BANANAS 0.55", "0.55", desc="BANANAS")], store="B MART", day="11/09/2026")
    categorize_all(conn, FakeCategorizer({"BNNA ORG": ("bananas", "produce", "high"),
                                          "BANANAS": ("bananas", "produce", "high")}))
    obs = db.observations(conn, "bananas")
    assert {(o.store, o.description, o.printed) for o in obs} == {
        ("A MART", "bananas", "BNNA ORG"), ("B MART", "bananas", "BANANAS")}
    [change] = price_changes(obs)
    assert (change.store, change.from_cents, change.to_cents) == ("A MART", 59, 69)
    [(key, rows)] = latest_by_store(obs).items()
    assert key == ("bananas", "each") and [o.store for o in rows] == ["B MART", "A MART"]  # cheapest first
    assert db.observations(conn, "BNNA")  # printed-name search still works


def test_variety_is_stored_and_checked(conn):
    rid = add(conn, [line("WHOLE MILK GAL 3.79", "3.79", desc="WHOLE MILK GAL"),
                     line("CFE GRD 12Z 8.99", "8.99", desc="CFE GRD 12Z")])
    categorize_receipt(conn, rid, FakeCategorizer({"WHOLE MILK GAL": ("milk", "dairy_eggs", "high", "whole"),
                                                   "CFE GRD 12Z": ("coffee", "beverages", "high", "ground 12oz")}))
    rows = {r["description_key"]: r for r in list_labels(conn)}
    assert (rows["WHOLE MILK GAL"]["normalized_name"], rows["WHOLE MILK GAL"]["variety"]) == ("milk", "whole")
    assert rows["CFE GRD 12Z"]["needs_review"] == 1 and "N5" in rows["CFE GRD 12Z"]["review_reasons"]


def test_percent_variety_is_allowed():
    assert review_reasons(ItemLabel(id=1, normalized_name="milk", variety="2%", pack_quantity=None, pack_unit=None,
                                    category="dairy_eggs", confidence="high")) == []


def test_known_names_are_offered_for_reuse(conn):
    cat = FakeCategorizer(ANSWERS)
    categorize_receipt(conn, add(conn, [MILK], store="A MART"), cat)
    categorize_receipt(conn, add(conn, [BNNA], store="B MART"), cat)
    assert cat.known_names == ["milk"]  # B MART's request saw A MART's names


def test_varieties_are_separate_price_series(conn):
    from pricelens.prices import price_changes
    add(conn, [line("MILK 1GAL 3.49", "3.49", desc="MILK 1GAL")], day="11/01/2026")
    add(conn, [line("WHOLE MILK 3.99", "3.99", desc="WHOLE MILK")], day="11/02/2026")
    categorize_all(conn, FakeCategorizer({"MILK 1GAL": ("milk", "dairy_eggs", "high"),
                                          "WHOLE MILK": ("milk", "dairy_eggs", "high", "whole")}))
    obs = db.observations(conn, "milk")
    assert len(obs) == 2 and price_changes(obs) == []  # not a 50c "price increase"


def test_human_fix_with_variety(conn, tmp_path):
    add(conn, [line("CHEDDAR BLK 4.29", "4.29", desc="CHEDDAR BLK")])
    fix_label(conn, "TEST MART", "CHEDDAR BLK", "cheddar cheese", "dairy_eggs", root=tmp_path, variety="block")
    row = list_labels(conn)[0]
    assert (row["normalized_name"], row["variety"], row["source"]) == ("cheddar cheese", "block", "human")
