from datetime import date

from pricelens.prices import Observation, effective_unit_prices, price_changes
from pricelens.verify import Line


def L(no, kind, total, qty=None, desc="X", applies=None, unit=None):
    return Line(no, desc, desc, kind, "items", qty, unit, None, total, applies)


def test_effective_price_includes_own_discount_only():
    lines = [L(1, "item", 500, desc="CHEESE"), L(2, "discount", -100, applies=1),
             L(3, "item", 300, desc="MILK"), L(4, "coupon", -200), L(5, "refund", -400), L(6, "fee", 10)]
    got = {l.description: p for l, p in effective_unit_prices(lines)}
    assert got == {"CHEESE": 400, "MILK": 300}


def test_effective_price_per_unit():
    lines = [L(1, "item", 300, qty=2, desc="YOGURT"), L(2, "item", 78, qty=1.32, unit="lb", desc="BANANAS")]
    got = {l.description: p for l, p in effective_unit_prices(lines)}
    assert got == {"YOGURT": 150, "BANANAS": 59}


def O(day, cents, store="S", desc="MILK", unit="each"):
    return Observation(store, desc, unit, date(2026, 1, day), cents)


def test_changes_up_and_down():
    ch = price_changes([O(1, 300), O(5, 349), O(9, 299)])
    assert [(c.from_cents, c.to_cents, c.change_cents) for c in ch] == [(300, 349, 49), (349, 299, -50)]
    assert round(ch[0].change_pct, 2) == 16.33


def test_unchanged_price_is_not_a_change():
    assert price_changes([O(1, 300), O(2, 300)]) == []


def test_order_independent():
    assert price_changes([O(9, 299), O(1, 300)])[0].from_day == date(2026, 1, 1)


def test_same_day_uses_lowest():
    ch = price_changes([O(1, 300), O(2, 350), O(2, 320)])
    assert ch[0].to_cents == 320


def test_stores_units_and_items_are_separate_series():
    obs = [O(1, 300, store="A"), O(2, 400, store="B"), O(1, 59, unit="lb"), O(2, 300, desc="BREAD")]
    assert price_changes(obs) == []


def test_single_observation():
    assert price_changes([O(1, 300)]) == []
