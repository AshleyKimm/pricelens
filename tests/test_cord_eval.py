"""CORD label normalization and scoring (no API, no dataset download needed)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
from cord import as_list, count, idr, load_truth, name, score  # noqa: E402


@pytest.mark.parametrize("raw,amount", [
    ("16,500", 16500), ("16.500", 16500), ("Rp 16.500", 16500), ("Rp. 16,500", 16500), ("RP16.500", 16500),
    ("16,500.00", 16500), ("16.500,00", 16500), ("@16,500", 16500), ("500", 500), ("1.250.000", 1250000),
    ("-5,000", -5000), (None, None), ("", None), ("abc", None), (["16,500"], 16500),
])
def test_idr_normalization(raw, amount):
    assert idr(raw) == amount


def test_menu_can_be_object_or_list():
    one = load_truth({"menu": {"nm": "EGG TART", "cnt": "1", "price": "13,000"}, "total": {"total_price": "13,000"}})
    many = load_truth({"menu": [{"nm": "EGG TART", "cnt": "1", "price": "13,000"},
                                {"nm": "PIZZA TOAST", "cnt": "2", "price": "32,000"}]})
    assert [i.name for i in one.items] == ["EGG TART"] and len(many.items) == 2
    assert as_list(None) == [] and as_list({"a": 1}) == [{"a": 1}]


def test_missing_subtotal_and_odd_fields():
    t = load_truth({"menu": [{"nm": ["ICE", "TEA"], "cnt": "2 x", "price": "Rp 10.000"}],
                    "total": {"total_price": "10,000"}})
    assert t.subtotal is None and t.total == 10000
    assert (t.items[0].name, t.items[0].count, t.items[0].price) == ("ICE TEA", 2.0, 10000)
    assert count("1.00") == 1.0 and count(None) is None and name("  egg   tart ") == "EGG TART"


TRUTH = load_truth({"menu": [{"nm": "REAL GANACHE", "cnt": "1", "price": "16,500"},
                             {"nm": "EGG TART", "cnt": "1", "price": "13,000"},
                             {"nm": "PIZZA TOAST", "cnt": "2", "price": "32,000"}],
                    "sub_total": {"subtotal_price": "61,500"}, "total": {"total_price": "61,500"}})


def L(n, p, c=None):
    return {"name": n, "count": c, "price": p}


def test_perfect_extraction():
    s = score(TRUTH, [L("REAL GANACHE", 16500, 1), L("EGG TART", 13000), L("PIZZA TOAST", 32000, 2)], 61500, 61500)
    assert (s.names_ok, s.prices_ok, s.counts_ok, s.subtotal_ok, s.total_ok, s.extra_lines) == (3, 3, 3, True, True, 0)
    assert s.failures == []


def test_errors_are_counted_and_listed():
    s = score(TRUTH, [L("REAL GANACHF", 16500, 1), L("EGG TART", 18000), L("SERVICE", 2000)], 61500, 66500)
    assert s.names_ok == 2                      # 'GANACHF' is close enough (>= 0.8)
    assert s.prices_ok == 1 and s.prices_scored == 3   # wrong egg tart price, pizza missing
    assert s.total_ok is False and s.subtotal_ok is True
    assert s.extra_lines == 1
    assert any("missing item 'PIZZA TOAST'" in f for f in s.failures)
    assert any("price of 'EGG TART'" in f for f in s.failures)


def test_price_breaks_ties_between_similar_names():
    t = load_truth({"menu": [{"nm": "ICED TEA", "price": "10,000"}, {"nm": "ICED TEA L", "price": "14,000"}]})
    s = score(t, [L("ICED TEA L", 14000), L("ICED TEA", 10000)], None, None)
    assert s.prices_ok == 2
