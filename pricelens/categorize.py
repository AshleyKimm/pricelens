"""Categorizers: anything with `label(store, items) -> LabelOutcome`.

One text-only request per receipt (all its uncached items), validated in code:
  - schema (enum categories, required fields)       -> MalformedOutputError
  - exactly the ids we sent, each once              -> MalformedOutputError (whole batch rejected)
  - per-item name/confidence checks                 -> needs_review on that item (categories.review_reasons)
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import Protocol

from pydantic import ValidationError

from . import errors
from .categories import CATEGORIES, ItemLabel, LabelBatch
from .models import strict_json_schema

PROMPT_VERSION = "categorize-v2"  # v2: base name + variety, reuse known names

SYSTEM_PROMPT = (
    "You label items from shopping receipts for a price tracker.\n\n"
    "For each item return a normalized_name, a variety and exactly one category.\n\nCategories:\n"
    + "\n".join(f"- {k}: {v}" for k, v in CATEGORIES.items())
    + """

Rules:
- normalized_name: the BASE product a shopper compares prices by, lowercase, in its common form:
  "bananas", "milk", "rice", "coffee", "olive oil", "chicken breast". No variety, brand, size,
  weight, count, codes or price.
- variety: what distinguishes it from the plain base product, lowercase ("organic", "whole",
  "jasmine", "extra virgin", "boneless skinless", "vanilla", "mint", "2%"), or "" if nothing.
  Examples: "ORG BNNA" -> bananas / organic; "WHOLE MILK GAL" -> milk / whole;
  "RICE JASMN" -> rice / jasmine; "EVOO 16.9OZ" -> olive oil / extra virgin; "MILK 1GAL" -> milk / "".
- If a list of known product names is given, reuse one of them exactly whenever it is the same
  base product, so the same product gets the same name at every store.
- Expand store abbreviations only when you are confident ("PNT BTR CRMY" -> "peanut butter").
- If you cannot tell what an item is, use category "uncertain", an empty normalized_name and
  confidence "low". Do not guess: an honest "uncertain" is better than a confident wrong answer.
- confidence "high" only if you would bet on both the name and the category.
- Discount, sale or return lines describe the product they refer to; label that product.
- Return every input id exactly once, and no other ids."""
)


@dataclass(frozen=True)
class ItemToLabel:
    id: int
    description: str
    raw_text: str


@dataclass
class LabelOutcome:
    labels: list[ItemLabel]
    model: str
    prompt_version: str
    raw_json: str


class Categorizer(Protocol):
    def label(self, store: str, items: list[ItemToLabel], known_names: list[str] = ()) -> LabelOutcome: ...


def user_message(store: str, items: list[ItemToLabel], known_names: list[str] = ()) -> str:
    payload = [{"id": i.id, "printed_name": i.description, "printed_line": i.raw_text} for i in items]
    known = f"Known product names: {json.dumps(sorted(known_names))}\n" if known_names else ""
    return f"Store: {store}\n{known}Items (JSON):\n{json.dumps(payload, ensure_ascii=False)}"


def parse_and_check(text: str, sent_ids: list[int]) -> LabelBatch:
    try:
        batch = LabelBatch.model_validate_json(text)
    except ValidationError as e:
        raise errors.MalformedOutputError(f"categorizer output failed schema validation: {e}") from e
    got = Counter(l.id for l in batch.items)
    missing = sorted(set(sent_ids) - set(got))
    extra = sorted(set(got) - set(sent_ids))
    dup = sorted(i for i, n in got.items() if n > 1)
    if missing or extra or dup:
        raise errors.MalformedOutputError(f"categorizer ids don't match: missing={missing} extra={extra} "
                                          f"duplicated={dup}")
    return batch


_SCHEMA = strict_json_schema(LabelBatch)


class GeminiCategorizer:
    def __init__(self, **client_kwargs):
        from .gemini import GeminiClient
        self.client = GeminiClient(**client_kwargs)
        self.trains_on_input = self.client.trains_on_input

    def label(self, store: str, items: list[ItemToLabel], known_names: list[str] = ()) -> LabelOutcome:
        text, model = self.client.generate_json(
            SYSTEM_PROMPT, [{"type": "text", "text": user_message(store, items, known_names)}], _SCHEMA)
        return LabelOutcome(parse_and_check(text, [i.id for i in items]).items, model, PROMPT_VERSION, text)


class ClaudeCategorizer:
    def __init__(self, **client_kwargs):
        from .extract import ClaudeClient
        self.client = ClaudeClient(**client_kwargs)

    def label(self, store: str, items: list[ItemToLabel], known_names: list[str] = ()) -> LabelOutcome:
        text, model = self.client.generate_json(
            SYSTEM_PROMPT, [{"type": "text", "text": user_message(store, items, known_names)}], _SCHEMA)
        return LabelOutcome(parse_and_check(text, [i.id for i in items]).items, model, PROMPT_VERSION, text)


class FakeCategorizer:
    """answers: description -> (normalized_name, category, confidence[, variety]); or an Exception to raise.
    `respond` can override the whole JSON reply (to test id checks)."""

    def __init__(self, answers: dict | Exception | None = None, respond=None, trains_on_input=False):
        self.answers = answers or {}
        self.respond = respond
        self.trains_on_input = trains_on_input
        self.calls: list[list[ItemToLabel]] = []

    def label(self, store: str, items: list[ItemToLabel], known_names: list[str] = ()) -> LabelOutcome:
        self.calls.append(items)
        self.known_names = list(known_names)
        if isinstance(self.answers, Exception):
            raise self.answers
        if self.respond:
            text = self.respond(items)
        else:
            out = []
            for i in items:
                name, cat, conf, *var = self.answers.get(i.description, ("", "uncertain", "low"))
                out.append({"id": i.id, "normalized_name": name, "variety": var[0] if var else "",
                            "category": cat, "confidence": conf})
            text = json.dumps({"items": out})
        return LabelOutcome(parse_and_check(text, [i.id for i in items]).items, "fake", "fake", text)
