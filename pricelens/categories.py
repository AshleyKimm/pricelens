"""The fixed category list and the categorizer's output contract.

The list is used as an `enum` in the structured-output schema, so the model cannot invent a
category. `other` = "I know what this is and it doesn't fit"; `uncertain` = "I can't tell what
this is". Only `uncertain` signals a reading problem.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CATEGORIES = {
    "produce": "fruit, vegetables, fresh herbs",
    "meat_seafood": "fresh or packaged meat, poultry, fish",
    "dairy_eggs": "milk, cheese, yogurt, butter, eggs, plant milks",
    "bakery": "bread, buns, pastries, tortillas",
    "deli_prepared": "deli meats, rotisserie chicken, ready meals",
    "frozen": "frozen foods of any kind",
    "pantry": "dry goods, canned, pasta, rice, oils, condiments, spices, baking",
    "snacks_sweets": "chips, crackers, candy, cookies, chocolate",
    "beverages": "water, soda, juice, coffee, tea (non-alcoholic)",
    "alcohol": "beer, wine, spirits",
    "household": "cleaning, paper goods, bags, kitchen supplies",
    "personal_care": "toiletries, cosmetics, toothpaste",
    "health": "medicine, vitamins, first aid",
    "baby_pet": "baby products and pet food/supplies",
    "restaurant_food": "food or drink ordered at a restaurant or cafe",
    "other": "clearly identifiable but none of the above",
    "uncertain": "you cannot tell what the item is",
}
Category = Literal[
    "produce", "meat_seafood", "dairy_eggs", "bakery", "deli_prepared", "frozen", "pantry",
    "snacks_sweets", "beverages", "alcohol", "household", "personal_care", "health", "baby_pet",
    "restaurant_food", "other", "uncertain",
]
Confidence = Literal["high", "medium", "low"]
MAX_NAME_LEN = 40
MAX_VARIETY_LEN = 30


class ItemLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int = Field(description="The id of the item, copied from the input.")
    normalized_name: str = Field(
        description="Base product, lowercase, the name a shopper compares prices by: 'rice', 'milk', "
        "'bananas'. No variety, brand, size, count, codes or price. Empty string if category is 'uncertain'."
    )
    variety: str = Field(
        description="What distinguishes this from the plain base product, lowercase: 'jasmine', 'whole', "
        "'organic', 'vanilla', '2%'. No brand, size or count. Empty string if nothing."
    )
    category: Category
    confidence: Confidence = Field(description="How sure you are of BOTH the name and the category.")


class LabelBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ItemLabel]


def description_key(text: str | None) -> str | None:
    """Cache key for a printed item name: uppercase, single spaces. Not raw_text, which includes the price."""
    if not text or not text.strip():
        return None
    return re.sub(r"\s+", " ", text.strip().upper())


def clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def name_problems(name: str, category: str, variety: str = "") -> list[str]:
    """Code checks on the model's normalized name and variety. Any problem -> needs_review."""
    if category == "uncertain":
        return []
    problems = []
    if len(variety) > MAX_VARIETY_LEN:
        problems.append(f"N4: variety longer than {MAX_VARIETY_LEN} characters")
    if re.search(r"\$|\d+\.\d\d|\b\d+\s*(oz|lb|ct|pk|ml|l|g|kg)\b", variety):
        problems.append("N5: variety contains a price or size")  # '2%' is allowed, '12oz' is not
    if not name:
        problems.append("N1: empty normalized name")
    if re.search(r"\d", name):
        problems.append("N2: name contains digits (size, code or price?)")
    if len(name) > MAX_NAME_LEN:
        problems.append(f"N3: name longer than {MAX_NAME_LEN} characters")
    return problems


def review_reasons(label: ItemLabel) -> list[str]:
    reasons = name_problems(clean_name(label.normalized_name), label.category, clean_name(label.variety))
    if label.category == "uncertain":
        reasons.append("C1: category uncertain")
    if label.confidence != "high":
        reasons.append(f"C2: confidence {label.confidence}")
    return reasons
