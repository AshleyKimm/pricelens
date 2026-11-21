"""Small builders so each test shows only what it is about."""
from pricelens.models import ExtractedLine, ExtractedReceipt


def line(raw="ITEM 1.00", total="1.00", kind="item", section="items", qty=None, unit=None,
         unit_price=None, desc=None, applies_to=None) -> ExtractedLine:
    return ExtractedLine(raw_text=raw, description=desc or raw.rsplit(" ", 1)[0], kind=kind, section=section,
                         quantity=qty, unit=unit, unit_price=unit_price, line_total=total,
                         applies_to_line=applies_to)


def receipt(lines, subtotal="1.00", tax=(), total="1.00", store="TEST MART", date_raw="11/18/2026",
            date_iso="2026-11-18", legibility="good", currency="USD") -> ExtractedReceipt:
    return ExtractedReceipt(store_name=store, date_raw=date_raw, date_iso=date_iso, currency=currency,
                            subtotal=subtotal, tax_lines=list(tax), total=total, legibility=legibility,
                            lines=lines)
