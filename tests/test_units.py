"""Pack sizes and unit prices: code check against printed text, conversions, and the two egg bugs."""
from datetime import date

import pytest
from helpers import line, receipt

from pricelens import db
from pricelens.categorize import FakeCategorizer
from pricelens.labeling import categorize_all, drop_outdated_ai_labels, list_labels
from pricelens.prices import comparable_by_unit_price, latest_by_store, price_changes
from pricelens.units import display, pack_problem, to_base
from pricelens.verify import verify

TODAY = date(2026, 11, 21)


@pytest.mark.parametrize("printed,qty,unit", [
    ("LG EGGS 24CT", 24, "count"), ("EGGS GRD A 6", 6, "count"), ("EGGS 1/2 DOZEN", 0.5, "dozen"),
    ("EGGS DOZEN", 1, "dozen"), ("HALF DOZEN EGGS", 0.5, "dozen"), ("EVOO 16.9OZ", 16.9, "fl oz"),
    ("MILK 1GAL", 1, "gal"), ("SPAG 16OZ", 16, "oz"), ("AVOCADO", None, None),
    ("WHOLE MILK GAL", 1, "gal"), ("GROUND BEEF LB", 1, "lb"),
])
def test_pack_sizes_backed_by_printed_text(printed, qty, unit):
    assert pack_problem(printed, qty, unit) is None


@pytest.mark.parametrize("printed,qty,unit", [
    ("LG EGGS 12CT", 24, "count"),     # model's number isn't printed
    ("EGGS GRD A 12", 2, "count"),     # '2' only inside '12' doesn't count
    ("EGGS 18CT", 1.5, "dozen"),       # dozen claimed but not printed
    ("EGGS 1/2 DOZEN", 2, "dozen"),
    ("AVOCADO", 4, None),              # half a pack size
    ("SPAG 16OZ", 16, "ounces"),       # unknown unit
    ("MILK 2GAL", 1, "gal"),           # a number IS printed, and it isn't 1
    ("WHOLE MILK GAL", 2, "gal"),      # bare word means 1, not 2
])
def test_unsupported_pack_sizes_are_flagged(printed, qty, unit):
    assert pack_problem(printed, qty, unit).startswith("P1")


def test_conversions_happen_in_code():
    assert to_base(0.5, "dozen") == (6.0, "count")
    assert to_base(16, "oz")[1] == "g" and round(to_base(16, "oz")[0]) == 454
    assert round(to_base(1, "gal")[0]) == 3785
    assert to_base(0, "count") is None and to_base(None, "g") is None


def test_display_units_default_imperial():
    per_egg, label = display(30.0, "count")
    assert (per_egg, label) == (30.0, "each")
    per_lb, label = display(1.0, "g")                 # 1 cent per gram
    assert label == "lb" and round(per_lb) == 454
    assert display(1.0, "g", "metric") == (100.0, "100 g")
    assert display(1.0, "ml", "metric") == (100.0, "100 ml")
    assert display(1.0, "ml")[1] == "gal"


# --- end to end with the egg-pack receipts' situation -------------------------------------------

EGGS = {"LG EGGS 12CT": ("eggs", "dairy_eggs", "high", "large", 12, "count"),
        "LG EGGS 6CT": ("eggs", "dairy_eggs", "high", "large", 6, "count"),
        "LG EGGS 24CT": ("eggs", "dairy_eggs", "high", "large", 24, "count"),
        "EGGS GRD A 6": ("eggs", "dairy_eggs", "high", "grade a", 6, "count"),
        "EGGS GRD A 30": ("eggs", "dairy_eggs", "high", "grade a", 30, "count"),
        "BANANAS 3LB": ("bananas", "produce", "high", "", 3, "lb"),
        "BANANAS": ("bananas", "produce", "high", "", None, None)}


def add(conn, store, day, *items):
    lines = [line(f"{d} {p}", p, desc=d) for d, p in items]
    total = sum(round(float(p) * 100) for _, p in items)
    amt = f"{total // 100}.{total % 100:02d}"
    res = verify(receipt(lines, subtotal=amt, total=amt, store=store, date_raw=day,
                         date_iso=f"2026-{day[:2]}-{day[3:5]}"), TODAY)
    db.save_receipt(conn, sha256=f"{store}{day}{items}", redacted_path=None, result=res, model="f",
                    prompt_version="f", extraction_json="{}", source="public")


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    add(c, "CORNER", "10/25/2026", ("LG EGGS 12CT", "4.69"))
    add(c, "CORNER", "11/14/2026", ("LG EGGS 6CT", "2.69"), ("LG EGGS 24CT", "7.99"))
    add(c, "MAPLE", "11/16/2026", ("EGGS GRD A 6", "2.29"), ("EGGS GRD A 30", "8.99"))
    categorize_all(c, FakeCategorizer(EGGS))
    return c


def test_switching_pack_size_is_not_a_price_change(conn):
    assert price_changes(db.observations(conn, "eggs")) == []  # was "-42.6%" before pack sizes


def test_cheapest_is_by_unit_price(conn):
    [(key, rows)] = latest_by_store(db.observations(conn, "eggs")).items()
    assert comparable_by_unit_price(rows)
    best = rows[0]
    assert (best.store, best.pack, round(best.base_price)) == ("MAPLE", "30 count", 30)  # 30c/egg, not the $2.29 6-pack


def test_weighed_and_packaged_compare_per_weight():
    c = db.connect(":memory:")
    # loose bananas priced per lb vs a printed 3 lb bag
    res = verify(receipt([line("BANANAS 1.50 lb @ 0.59/lb 0.89", "0.89", desc="BANANAS", qty=1.5, unit="lb",
                               unit_price="0.59")], subtotal="0.89", total="0.89", store="A"), TODAY)
    db.save_receipt(c, sha256="w", redacted_path=None, result=res, model="f", prompt_version="f",
                    extraction_json="{}", source="public")
    add(c, "B", "11/18/2026", ("BANANAS 3LB", "1.49"))
    categorize_all(c, FakeCategorizer(EGGS))
    obs = db.observations(c, "bananas")
    assert {o.base_unit for o in obs} == {"g"}
    per_lb = {o.store: round(display(o.base_price, o.base_unit)[0]) for o in obs}
    assert per_lb == {"A": 59, "B": 50}  # bag is cheaper per lb


def test_bad_pack_size_is_flagged_and_not_used():
    c = db.connect(":memory:")
    add(c, "CORNER", "11/14/2026", ("LG EGGS 6CT", "2.69"))
    categorize_all(c, FakeCategorizer({"LG EGGS 6CT": ("eggs", "dairy_eggs", "high", "large", 12, "count")}))
    [row] = list_labels(c)
    assert row["needs_review"] == 1 and "P1" in row["review_reasons"]
    assert row["pack_quantity"] == 12 and row["pack_base_qty"] is None  # kept for audit, not used
    assert db.observations(c, "eggs")[0].base_price is None


def test_outdated_ai_labels_are_relabeled_but_human_ones_kept(conn, tmp_path):
    from pricelens.labeling import fix_label
    fix_label(conn, "CORNER", "LG EGGS 6CT", "eggs", "dairy_eggs", root=tmp_path, variety="large")
    conn.execute("UPDATE item_labels SET prompt_version = 'categorize-v2' WHERE source = 'ai'")
    dropped = drop_outdated_ai_labels(conn, "categorize-v3")
    assert dropped == 4 and [r["source"] for r in list_labels(conn)] == ["human"]



def test_usd_and_cad_are_never_compared():
    c = db.connect(":memory:")
    for store, cur, price in [("US MART", "USD", "3.49"), ("CA MART", "CAD", "4.29")]:
        res = verify(receipt([line(f"MILK {price}", price, desc="MILK")], subtotal=price, total=price, store=store,
                             currency=cur), TODAY)
        db.save_receipt(c, sha256=store, redacted_path=None, result=res, model="f", prompt_version="f",
                        extraction_json="{}", source="public")
    categorize_all(c, FakeCategorizer({"MILK": ("milk", "dairy_eggs", "high")}))
    groups = latest_by_store(db.observations(c, "milk"))
    assert len(groups) == 2 and all(len(rows) == 1 for rows in groups.values())  # separate, not ranked together
    assert {o.currency for o in db.observations(c, "milk")} == {"USD", "CAD"}


def test_extraction_prompt_tells_cad_from_usd():
    from pricelens.extract import PROMPT_VERSION, SYSTEM_PROMPT
    assert "CAD" in SYSTEM_PROMPT and "GST" in SYSTEM_PROMPT and PROMPT_VERSION == "extract-v2"


def test_recheck_applies_improved_rules_without_api_calls():
    from pricelens.labeling import recheck_pack_sizes
    c = db.connect(":memory:")
    add(c, "CORNER", "11/14/2026", ("WHOLE MILK GAL", "3.79"))
    categorize_all(c, FakeCategorizer({"WHOLE MILK GAL": ("milk", "dairy_eggs", "high", "whole", 1, "gal")}))
    c.execute("UPDATE item_labels SET pack_base_qty = NULL, pack_base_unit = NULL, needs_review = 1, "
              "review_reasons = '[\"P1: old rule\"]'")                           # as labeled under the old rule
    assert recheck_pack_sizes(c) == 1
    [row] = list_labels(c)
    assert round(row["pack_base_qty"]) == 3785 and row["needs_review"] == 0
    assert recheck_pack_sizes(c) == 0                                            # idempotent
