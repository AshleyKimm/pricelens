"""Web layer: pages, chart, corrections through the form, and demo mode's read-only guarantees."""
import json
import shutil
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import line, receipt

from pricelens import db
from pricelens.categorize import FakeCategorizer
from pricelens.labeling import categorize_all
from pricelens.verify import verify
from pricelens.web.app import create_app
from pricelens.web.chart import Series, legend_html, line_chart_svg, nice_ticks

REPO = Path(__file__).resolve().parents[1]
TODAY = date(2026, 11, 21)
ANSWERS = {"MILK 1GAL": ("milk", "dairy_eggs", "high"), "WHOLE MILK": ("milk", "dairy_eggs", "high", "whole"),
           "BNNA ORG": ("bananas", "produce", "high", "organic")}


def _add(conn, lines, store, day, sub=None, sha=None):
    total = sum(round(float(l.line_total) * 100) for l in lines)
    amt = f"{total // 100}.{total % 100:02d}"
    ext = receipt(lines, subtotal=sub or amt, total=amt, store=store, date_raw=day,
                  date_iso=f"2026-{day[:2]}-{day[3:5]}")
    return db.save_receipt(conn, sha256=sha or f"{'%064x' % abs(hash((store, day, sub)))}", redacted_path=None,
                           result=verify(ext, TODAY), model="fake", prompt_version="fake", extraction_json="{}",
                           source="public")


@pytest.fixture
def root(tmp_path):
    """A tiny database: milk at two stores with a price change, one flagged receipt."""
    conn = db.connect(tmp_path / "data" / "pricelens.db")
    _add(conn, [line("MILK 1GAL 3.49", "3.49", desc="MILK 1GAL")], "A MART", "11/01/2026")
    _add(conn, [line("MILK 1GAL 3.79", "3.79", desc="MILK 1GAL")], "A MART", "11/08/2026")
    _add(conn, [line("WHOLE MILK 3.29", "3.29", desc="WHOLE MILK"),
                line("BNNA ORG 0.78", "0.78", desc="BNNA ORG")], "B MART", "11/09/2026")
    _add(conn, [line("MILK 1GAL 0.99", "0.99", desc="MILK 1GAL")], "A MART", "11/10/2026", sub="9.99")  # flagged
    categorize_all(conn, FakeCategorizer(ANSWERS))
    conn.close()
    return tmp_path


@pytest.fixture
def local(root):
    return TestClient(create_app("local", root=root))


@pytest.fixture
def demo(root):
    shutil.copy(root / "data" / "pricelens.db", root / "data" / "demo.db")
    return TestClient(create_app("demo", root=root))


def test_search_lists_products_across_stores(local):
    r = local.get("/", params={"q": "milk"})
    assert r.status_code == 200
    assert "milk" in r.text and "2</td>" in r.text  # 2 stores
    assert "$3.29" in r.text                          # lowest latest price (B MART whole milk)


def test_search_escapes_input(local):
    r = local.get("/", params={"q": "<script>alert(1)</script>"})
    assert "<script>alert(1)</script>" not in r.text and "&lt;script&gt;" in r.text


def test_security_headers(local):
    h = local.get("/").headers
    assert "script-src" not in h["content-security-policy"] and "default-src 'none'" in h["content-security-policy"]
    assert h["x-content-type-options"] == "nosniff"


def test_product_page(local):
    r = local.get("/product", params={"name": "milk"})
    assert r.status_code == 200
    t = r.text
    assert "<svg" in t and 'class="line s1"' in t           # A MART has 2 points -> a line
    assert 'class="legend"' in t                             # 2 stores -> legend
    assert "up" in t and "$3.49" in t and "$3.79" in t       # price change shown
    assert "$0.99" not in t                                  # flagged receipt excluded
    assert local.get("/product", params={"name": "unicorn"}).status_code == 404


def test_receipt_pages_show_why(local):
    r = local.get("/receipts")
    assert "needs review" in r.text and "V1" in r.text
    flagged = [row for row in r.text.split("<tr>") if "needs review" in row][0]
    rid = flagged.split('href="/receipts/')[1].split('"')[0]
    d = local.get(f"/receipts/{rid}")
    assert d.status_code == 200 and "FAIL V1" in d.text and "$9.99" in d.text


def test_images_served_by_hash_only(local, root):
    conn = db.connect(root / "data" / "pricelens.db")
    sha = conn.execute("SELECT image_sha256 FROM receipts WHERE id = 1").fetchone()[0]
    (root / "data" / "redacted").mkdir(parents=True)
    (root / "data" / "redacted" / f"{sha}.jpg").write_bytes(b"\xff\xd8fake")
    r = local.get("/receipts/1/image")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert local.get("/receipts/2/image").status_code == 404    # file not there
    assert local.get("/receipts/999/image").status_code == 404
    assert local.get("/receipts/..%2F..%2Fsecret/image").status_code in (404, 422)


def test_correct_a_label_through_the_form(local, root):
    r = local.post("/labels/fix", data={"store": "B MART", "description": "BNNA ORG", "name": "bananas",
                                        "variety": "", "category": "produce"}, follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    page = local.get(r.headers["location"]).text
    assert "human" in page and "Saved" in page
    cases = (root / "evals/synthetic/label_corrections.jsonl").read_text().splitlines()
    assert json.loads(cases[-1])["ai_previous"]["variety"] == "organic"
    assert "✓ yes" in page  # correction listed and still applied


def test_invalid_correction_is_rejected_with_message(local):
    r = local.post("/labels/fix", data={"store": "B MART", "description": "BNNA ORG", "name": "bananas 2lb",
                                        "category": "produce"})
    assert "N2" in r.text  # followed the redirect; error shown
    r = local.post("/labels/fix", data={"store": "B MART"})
    assert "missing" in r.text


def test_cross_origin_post_refused(local):
    r = local.post("/labels/fix", data={"store": "B MART", "description": "BNNA ORG", "name": "x",
                                        "category": "produce"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_demo_mode_has_no_write_routes(demo):
    page = demo.get("/labels").text
    assert "<form" not in page and "read-only" in page
    r = demo.post("/labels/fix", data={"store": "B MART", "description": "BNNA ORG", "name": "x",
                                       "category": "produce"})
    assert r.status_code in (404, 405)


def test_demo_database_connection_refuses_writes(root):
    from pricelens.web.app import connect_readonly
    shutil.copy(root / "data" / "pricelens.db", root / "data" / "demo.db")
    conn = connect_readonly(root / "data" / "demo.db")
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM receipts")


def test_demo_runs_without_api_keys(root, monkeypatch):
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    shutil.copy(root / "data" / "pricelens.db", root / "data" / "demo.db")
    c = TestClient(create_app("demo", root=root))
    assert c.get("/product", params={"name": "milk"}).status_code == 200


def test_demo_requires_a_built_database(tmp_path):
    c = TestClient(create_app("demo", root=tmp_path), raise_server_exceptions=False)
    assert c.get("/").status_code == 500  # clear error in logs: run `demo build`


def test_committed_demo_rebuilds_identically(tmp_path):
    from pricelens import demo as demo_mod
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    assert demo_mod.build(REPO, a) == demo_mod.build(REPO, b)
    snap = lambda p: sorted(sqlite3.connect(p).execute(
        "SELECT image_sha256, status, total_cents, review_reasons FROM receipts"))
    n = len(list((REPO / 'demo' / 'extractions').glob('*.json')))
    assert snap(a) == snap(b) and len(snap(a)) == n >= 18


# --- chart ---------------------------------------------------------------------

def test_nice_ticks():
    assert nice_ticks(0, 100) == [0, 25, 50, 75, 100]
    t = nice_ticks(312, 389)
    assert t[0] <= 312 and t[-1] >= 389 and len(t) <= 7


def test_chart_escapes_and_labels():
    s = [Series("<b>A</b>", 1, [(date(2026, 1, 1), 300), (date(2026, 1, 8), 350)]),
         Series("B", 2, [(date(2026, 1, 3), 200)])]
    svg = line_chart_svg(s)
    assert "<b>" not in svg and "&lt;b&gt;" in svg
    assert svg.count("<path") == 1 and svg.count("<circle") == 3  # single-point series: marker only
    assert 'class="label"' in svg                                  # far apart -> direct labels
    assert "<title>" in svg                                        # hover detail without JS
    assert legend_html(s).count("<li>") == 2 and legend_html(s[:1]) == ""


def test_colliding_end_labels_are_dropped():
    s = [Series("A", 1, [(date(2026, 1, 1), 300)]), Series("B", 2, [(date(2026, 1, 1), 301)])]
    assert 'class="label"' not in line_chart_svg(s)


def test_empty_chart():
    assert line_chart_svg([]) == ""


def test_tie_has_no_cheapest_badge_and_variety_note_is_specific(root):
    conn = db.connect(root / "data" / "pricelens.db")
    _add(conn, [line("MILK 1GAL 3.29", "3.29", desc="MILK 1GAL")], "A MART", "11/11/2026")  # ties B MART
    conn.close()
    t = TestClient(create_app("local", root=root)).get("/product", params={"name": "milk"}).text
    assert "cheapest</span>" not in t
    assert "plain vs. whole" in t or "whole vs. plain" in t


def test_tied_lowest_price_lists_the_stores(root):
    conn = db.connect(root / "data" / "pricelens.db")
    _add(conn, [line("MILK 1GAL 3.29", "3.29", desc="MILK 1GAL")], "A MART", "11/11/2026")  # ties B MART's 3.29
    conn.close()
    t = TestClient(create_app("local", root=root)).get("/", params={"q": "milk"}).text
    assert "<details class=\"stores\">" in t and "2 stores</span></summary>" in t
    block = t.split('<details class="stores">')[1].split("</details>")[0]
    assert "A MART" in block and "B MART" in block and "(whole)" in block


def test_single_cheapest_store_is_named_directly(local):
    t = local.get("/", params={"q": "milk"}).text
    assert "<details" not in t and "B MART</span>" in t


def test_same_number_different_unit_is_not_a_tie(root):
    from pricelens.web import views
    from pricelens.prices import Observation
    obs = [Observation("A", "bananas", "lb", date(2026, 11, 1), 59), Observation("B", "bananas", "each", date(2026, 11, 1), 59)]
    import pricelens.db as dbm
    orig = dbm.observations
    dbm.observations = lambda conn, q: obs
    try:
        [row] = views.search(None, "bananas")
    finally:
        dbm.observations = orig
    assert len(row.cheapest_at) == 1


# --- unit prices and the units switch -------------------------------------------------------------

@pytest.fixture
def eggs(tmp_path):
    from helpers import line as L
    conn = db.connect(tmp_path / "data" / "pricelens.db")
    _add(conn, [L("LG EGGS 6CT 2.69", "2.69", desc="LG EGGS 6CT"), L("LG EGGS 24CT 7.99", "7.99", desc="LG EGGS 24CT")],
         "A MART", "11/14/2026")
    _add(conn, [L("EGGS 30 8.99", "8.99", desc="EGGS 30"), L("SPAG 16OZ 1.79", "1.79", desc="SPAG 16OZ")],
         "B MART", "11/16/2026")
    categorize_all(conn, FakeCategorizer({
        "LG EGGS 6CT": ("eggs", "dairy_eggs", "high", "", 6, "count"),
        "LG EGGS 24CT": ("eggs", "dairy_eggs", "high", "", 24, "count"),
        "EGGS 30": ("eggs", "dairy_eggs", "high", "", 30, "count"),
        "SPAG 16OZ": ("spaghetti", "pantry", "high", "", 16, "oz")}))
    conn.close()
    return TestClient(create_app("local", root=tmp_path))


def test_best_value_is_by_unit_price(eggs):
    t = eggs.get("/product", params={"name": "eggs"}).text
    best = t.split('class="best"')[1].split("</tr>")[0]
    assert "B MART" in best and "best value" in best and "$0.30 / each" in best   # not the $2.69 6-pack
    home = eggs.get("/", params={"q": "eggs"}).text
    assert "$0.30 / each" in home and "(30 count)" in home


def test_units_switch_changes_display_and_is_remembered(eggs):
    assert "$1.79 / lb" in eggs.get("/", params={"q": "spaghetti"}).text      # imperial by default (16 oz = 1 lb)
    r = eggs.get("/units", params={"u": "metric", "next": "/?q=spaghetti"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?q=spaghetti"
    assert "units=metric" in r.headers["set-cookie"] and "httponly" in r.headers["set-cookie"].lower()
    eggs.cookies.set("units", "metric")
    assert "$0.39 / 100 g" in eggs.get("/", params={"q": "spaghetti"}).text   # 179c / 453.6 g * 100


@pytest.mark.parametrize("bad", ["//evil.example/x", "https://evil.example", "evil.example", "/\evil.example"])
def test_units_switch_cannot_redirect_off_site(eggs, bad):
    r = eggs.get("/units", params={"u": "metric", "next": bad}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_units_switch_rejects_unknown_system_and_works_in_demo(root):
    shutil.copy(root / "data" / "pricelens.db", root / "data" / "demo.db")
    c = TestClient(create_app("demo", root=root))
    assert c.get("/units", params={"u": "furlongs"}).status_code == 400
    assert c.get("/units", params={"u": "metric"}, follow_redirects=False).status_code == 303
