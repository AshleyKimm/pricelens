"""The extraction contract: what the model must return.

One Pydantic model serves as (1) the JSON schema sent to the API as a structured-output
constraint, (2) runtime validation of the response, and (3) test fixtures.

Money fields are STRINGS exactly as printed; code converts them to cents (see money.py).
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Kind = Literal["item", "discount", "coupon", "refund", "fee"]
Section = Literal["items", "after_subtotal"]
Unit = Literal["each", "lb", "kg", "oz", "g"]
Legibility = Literal["good", "partial", "poor"]


class ExtractedLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_text: str = Field(description="The line exactly as printed, including codes and price.")
    description: Optional[str] = Field(description="Item name as printed, without codes or price.")
    kind: Kind = Field(
        description="item=product; discount=reduction tied to one item; coupon=store/manufacturer "
        "coupon or member savings not tied to one item; refund=returned item; fee=bag fee, "
        "bottle deposit, etc."
    )
    section: Section = Field(
        description="'items' if printed above the SUBTOTAL line, 'after_subtotal' if printed "
        "between SUBTOTAL and TOTAL (excluding tax lines)."
    )
    quantity: Optional[float] = Field(description="Count or weight. null if not printed.")
    unit: Optional[Unit] = Field(description="'each' for counted items, or the weight unit.")
    unit_price: Optional[str] = Field(description="Per-unit price as printed, e.g. '0.59'. null if not printed.")
    line_total: Optional[str] = Field(
        description="Amount in the price column exactly as printed, keeping any minus sign, "
        "trailing '-' or 'CR'. null if unreadable."
    )
    applies_to_line: Optional[int] = Field(
        description="For kind=discount: the 1-based index in `lines` of the item it reduces."
    )


class ExtractedReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_name: Optional[str] = Field(description="Store name as printed in the header.")
    date_raw: Optional[str] = Field(description="Purchase date exactly as printed (may include time).")
    date_iso: Optional[str] = Field(description="Same date as YYYY-MM-DD, or null if unsure.")
    currency: str = Field(description="ISO 4217 code, e.g. USD.")
    subtotal: Optional[str] = Field(description="SUBTOTAL exactly as printed. null if not printed. Do not calculate.")
    tax_lines: list[str] = Field(description="Each printed tax amount. Empty if none. Do not calculate.")
    total: Optional[str] = Field(description="TOTAL / BALANCE DUE exactly as printed. Do not calculate.")
    legibility: Legibility = Field(
        description="good=all text readable; partial=some values unreadable; poor=most unreadable."
    )
    lines: list[ExtractedLine] = Field(
        description="Every purchase line in printed order (items, discounts, coupons, refunds, fees). "
        "Exclude subtotal, tax, total, payment, change, and loyalty/card lines."
    )


def strict_json_schema(model: type[BaseModel]) -> dict:
    """Pydantic JSON schema -> the stricter shape structured outputs expects.

    Inlines $defs, drops titles/defaults, sets additionalProperties=false and marks every
    property required (nullable fields stay nullable via anyOf).
    """
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            out = {k: walk(v) for k, v in node.items() if k not in ("title", "default")}
            if out.get("type") == "object" and "properties" in out:
                out["additionalProperties"] = False
                out["required"] = list(out["properties"].keys())
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)
