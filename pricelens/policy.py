"""Data policy, enforced in code.

Extractors whose provider may train on or human-review inputs (e.g. Gemini free tier) may
only receive images from folders that hold public or fake data. Real receipts are refused
whichever command is used.
"""
from __future__ import annotations

from pathlib import Path

from .errors import PriceLensError

PUBLIC_DATA_DIRS = ("evals/data/cord", "evals/synthetic", "demo/receipts")


class DataPolicyError(PriceLensError):
    pass


def is_public_data(path: Path, root: Path | None = None) -> bool:
    root = (root or Path.cwd()).resolve()
    p = Path(path).resolve()
    return any(p.is_relative_to(root / d) for d in PUBLIC_DATA_DIRS)


def check_allowed(extractor, path: Path, root: Path | None = None) -> None:
    if getattr(extractor, "trains_on_input", False) and not is_public_data(path, root):
        raise DataPolicyError(
            f"{type(extractor).__name__} uses a provider tier that may train on or human-review inputs, "
            f"so it only accepts images under {', '.join(PUBLIC_DATA_DIRS)}. "
            "Use a no-training provider/tier for real receipts."
        )
