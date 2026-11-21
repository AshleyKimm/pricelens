import pytest
"""Totals check and the other verification rules, one edge case per test."""
from datetime import date

from helpers import line, receipt

from pricelens.verify import find_sensitive_text, line_tolerance_cents, scrub_text, verify

TODAY = date(2026, 11, 20)


def codes(result, blocking=True):
    return sorted(i.code for i in result.issues if i.blocking == blocking)


def test_clean_receipt_verifies():
    r = verify(receipt([line("MILK 3.49", "3.49"), line("BREAD 2.51", "2.51")],
                       subtotal="6.00", tax=["0.30"], total="6.30"), TODAY)
    assert r.status == "verified" and r.issues == []
    assert r.receipt.subtotal_cents == 600 and r.receipt.tax_cents == 30


def test_items_not_matching_subtotal_flags_v1():
    r = verify(receipt([line("MILK 3.49", "3.49")], subtotal="4.49", total="4.49"), TODAY)
    assert r.status == "needs_review" and codes(r) == ["V1"]


def test_missing_item_also_breaks_total():
    # A dropped line: items sum short of subtotal; subtotal + tax still equals total.
    r = verify(receipt([line("A 1.00", "1.00")], subtotal="3.00", tax=["0.10"], total="3.10"), TODAY)
    assert codes(r) == ["V1"]


def test_subtotal_plus_tax_not_total_flags_v2():
    r = verify(receipt([line("A 1.00", "1.00")], subtotal="1.00", tax=["0.08"], total="1.10"), TODAY)
    assert codes(r) == ["V2"]


def test_one_cent_off_is_a_failure():
    r = verify(receipt([line("A 1.00", "1.00")], subtotal="1.01", total="1.01"), TODAY)
    assert codes(r) == ["V1"]


def test_multiple_tax_lines_are_summed():
    r = verify(receipt([line("A 10.00", "10.00")], subtotal="10.00", tax=["0.50", "0.25"], total="10.75"), TODAY)
    assert r.status == "verified"


def test_item_discount_is_negative_even_if_model_drops_sign():
    lines = [line("CHEESE 5.00", "5.00"), line("SALE 1.00", "1.00", kind="discount", applies_to=1)]
    r = verify(receipt(lines, subtotal="4.00", total="4.00"), TODAY)
    assert r.status == "verified"
    assert r.receipt.lines[1].line_total_cents == -100


def test_discount_must_point_at_an_item_v8():
    lines = [line("CHEESE 5.00", "5.00"), line("SALE 1.00-", "1.00-", kind="discount", applies_to=7)]
    assert codes(verify(receipt(lines, subtotal="4.00", total="4.00"), TODAY)) == ["V8"]


def test_coupon_after_subtotal_counts_toward_total():
    lines = [line("A 10.00", "10.00"), line("STORE COUPON 2.00-", "2.00-", kind="coupon", section="after_subtotal")]
    r = verify(receipt(lines, subtotal="10.00", tax=["0.80"], total="8.80"), TODAY)
    assert r.status == "verified"


def test_coupon_above_subtotal_counts_toward_subtotal():
    lines = [line("A 10.00", "10.00"), line("MEMBER SAVINGS 1.50", "1.50", kind="coupon")]
    assert verify(receipt(lines, subtotal="8.50", total="8.50"), TODAY).status == "verified"


def test_refund_is_negative():
    lines = [line("A 10.00", "10.00"), line("RETURN B 4.00", "4.00", kind="refund", qty=1, unit_price="4.00")]
    r = verify(receipt(lines, subtotal="6.00", total="6.00"), TODAY)
    assert r.status == "verified" and r.receipt.lines[1].line_total_cents == -400


def test_fee_after_subtotal():
    lines = [line("A 5.00", "5.00"), line("BAG FEE 0.10", "0.10", kind="fee", section="after_subtotal")]
    assert verify(receipt(lines, subtotal="5.00", total="5.10"), TODAY).status == "verified"


def test_multibuy_quantity_times_unit_price():
    lines = [line("2 @ 1.50 YOGURT 3.00", "3.00", qty=2, unit="each", unit_price="1.50")]
    assert verify(receipt(lines, subtotal="3.00", total="3.00"), TODAY).status == "verified"


def test_three_for_five_rounding_is_tolerated():
    # 3 x 1.67 = 5.01 but the line prints 5.00: allowed (unit price was rounded).
    lines = [line("3 @ 3/5.00 LIMES 5.00", "5.00", qty=3, unit="each", unit_price="1.67")]
    assert verify(receipt(lines, subtotal="5.00", total="5.00"), TODAY).status == "verified"


def test_wrong_line_math_flags_v4():
    lines = [line("2 @ 1.50 YOGURT 4.00", "4.00", qty=2, unit="each", unit_price="1.50")]
    assert codes(verify(receipt(lines, subtotal="4.00", total="4.00"), TODAY)) == ["V4"]


def test_weighed_item_within_tolerance():
    # 1.32 lb x 0.59 = 0.7788 -> printed 0.78
    lines = [line("BANANAS 1.32 lb @ 0.59/lb 0.78", "0.78", qty=1.32, unit="lb", unit_price="0.59")]
    assert verify(receipt(lines, subtotal="0.78", total="0.78"), TODAY).status == "verified"


def test_half_cent_rounds_up_like_a_register():
    # 1.5 lb x 0.59 = 88.5c -> 89c (Python's round() would give 88)
    lines = [line("BANANAS 1.50 lb @ 0.59/lb 0.89", "0.89", qty=1.5, unit="lb", unit_price="0.59")]
    assert verify(receipt(lines, subtotal="0.89", total="0.89"), TODAY).status == "verified"


def test_weighed_items_allow_one_cent():
    lines = [line("BANANAS 1.50 lb @ 0.59/lb 0.90", "0.90", qty=1.5, unit="lb", unit_price="0.59")]
    assert verify(receipt(lines, subtotal="0.90", total="0.90"), TODAY).status == "verified"
    lines = [line("BANANAS 1.50 lb @ 0.59/lb 0.91", "0.91", qty=1.5, unit="lb", unit_price="0.59")]
    assert "V4" in codes(verify(receipt(lines, subtotal="0.91", total="0.91"), TODAY))


def test_tolerance_rule():
    assert line_tolerance_cents(1) == 0
    assert line_tolerance_cents(1.32) == 0
    assert line_tolerance_cents(2) == 1
    assert line_tolerance_cents(3) == 1


def test_no_subtotal_printed_uses_v3():
    r = verify(receipt([line("A 1.00", "1.00")], subtotal=None, tax=["0.10"], total="1.10"), TODAY)
    assert r.status == "verified" and codes(r, blocking=False) == ["V3"]
    r = verify(receipt([line("A 1.00", "1.00")], subtotal=None, tax=["0.10"], total="1.20"), TODAY)
    assert codes(r) == ["V3"]


def test_unreadable_values_are_flagged_not_guessed():
    r = verify(receipt([line("A ?.??", None)], subtotal="1.00", total="1.00"), TODAY)
    assert "V6" in codes(r) and "V1" not in codes(r)  # don't report a sum we can't compute


def test_garbled_amount_is_flagged():
    r = verify(receipt([line("A 1.0O", "1.0O")], subtotal="1.00", total="1.00"), TODAY)
    assert "V6" in codes(r)


def test_missing_store_and_total():
    r = verify(receipt([line()], store=None, total=None), TODAY)
    assert codes(r) == ["V6", "V6"]


def test_poor_legibility_flags_v7():
    assert "V7" in codes(verify(receipt([line()], legibility="poor"), TODAY))


def test_model_and_code_disagree_on_date():
    r = verify(receipt([line()], date_raw="11/18/2026", date_iso="2026-11-19"), TODAY)
    assert codes(r) == ["V5"]


def test_ambiguous_date_is_a_note():
    r = verify(receipt([line()], date_raw="03/04/26", date_iso="2026-03-04"), TODAY)
    assert r.status == "verified" and codes(r, blocking=False) == ["V5"]


def test_future_and_too_old_dates():
    assert codes(verify(receipt([line()], date_raw="12/25/2026", date_iso="2026-12-25"), TODAY)) == ["V5"]
    assert codes(verify(receipt([line()], date_raw="01/02/2019", date_iso="2019-01-02"), TODAY)) == ["V5"]


def test_unparseable_date():
    assert codes(verify(receipt([line()], date_raw="??", date_iso=None), TODAY)) == ["V5"]


def test_idr_receipt():
    lines = [line("NASI GORENG 25.000", "25.000"), line("ES TEH 5.000", "5.000")]
    r = verify(receipt(lines, subtotal="30.000", tax=["3.000"], total="Rp 33.000", currency="IDR"), TODAY)
    assert r.receipt.total_cents == 33000          # still parsed correctly (the CORD eval relies on this)...
    assert codes(r) == ["V9"]                      # ...but the app only accepts USD and CAD


def test_cad_receipt_is_accepted():
    r = verify(receipt([line("MILK 4.29", "4.29")], subtotal="4.29", tax=["0.00"], total="4.29", currency="cad"), TODAY)
    assert r.status == "verified"


@pytest.mark.parametrize("cur", ["EUR", "IDR", "", "US"])
def test_other_currencies_are_flagged(cur):
    r = verify(receipt([line()], currency=cur), TODAY)
    assert "V9" in codes(r)


def test_card_number_in_output_is_flagged_and_scrubbed():
    lines = [line("A 1.00", "1.00"), line("VISA 4111 1111 1111 1111 1.00", "0.00", kind="fee", section="after_subtotal")]
    r = verify(receipt(lines, subtotal="1.00", total="1.00"), TODAY)
    assert "S1" in codes(r)
    assert "4111" not in scrub_text("VISA 4111 1111 1111 1111")


def test_sensitive_text_rules():
    assert find_sensitive_text("4111111111111111")            # Luhn-valid 16 digits
    assert not find_sensitive_text("4111111111111112")        # fails Luhn
    assert not find_sensitive_text("012345678905 BANANAS")    # 12-digit UPC
    assert find_sensitive_text("MEMBER # 123456789")
    assert not find_sensitive_text("MEMBER SAVINGS -1.00")
    assert scrub_text("LOYALTY 98765432 SAVED 1.00") == "LOYALTY [REDACTED] SAVED 1.00"
