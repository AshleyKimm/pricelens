"""Extractors: anything with `extract(image) -> ExtractionResult`.

The pipeline depends only on the Extractor protocol, so adding a new model/provider needs no
pipeline changes.
  ClaudeExtractor  - live API call (vision + structured output)
  ReplayExtractor  - returns saved responses keyed by image hash (public demo, free re-runs)
  FakeExtractor    - canned results/exceptions for tests
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from . import errors
from .models import ExtractedReceipt, strict_json_schema
from .redact import PreparedImage

PROMPT_VERSION = "extract-v2"  # v2: currency hint (USD vs CAD)
DEFAULT_MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = """You transcribe store receipts into structured data for a price tracker.

Your output is checked by code: printed line totals must add up to the printed SUBTOTAL, and
SUBTOTAL + tax + post-subtotal adjustments must equal the printed TOTAL. So:
- Copy subtotal, tax, and total exactly as printed. Never calculate or "fix" them.
- Copy every amount exactly as printed, including minus signs, trailing '-' and 'CR'.
- If something is unreadable, use null. Do not guess. A null is better than a wrong value.
- Include every purchase line in printed order: items, item discounts, coupons/member
  savings, refunds/returns, fees and deposits. A multi-line item (e.g. "2 @ 1.50" on the line
  under the name) is ONE line whose raw_text contains both printed lines.
- Do not transcribe payment, card, account, loyalty or member numbers anywhere.
- Report legibility honestly.
- currency: the ISO code. Canadian receipts also print "$": use CAD when you see GST/HST/PST/QST tax
  lines, a Canadian province or postal code (e.g. "ON", "M5V 2T6") or "CAD"; otherwise USD for "$"."""

USER_PROMPT = "Extract this receipt."


@dataclass
class ExtractionResult:
    receipt: ExtractedReceipt
    raw_json: str        # exact model output text, stored for audit/re-verification
    model: str           # model that actually served the request (may differ after a fallback)
    prompt_version: str


class Extractor(Protocol):
    def extract(self, image: PreparedImage) -> ExtractionResult: ...


def parse_output(text: str) -> ExtractedReceipt:
    """Validate model output against the schema before anything is stored."""
    try:
        return ExtractedReceipt.model_validate_json(text)
    except ValidationError as e:
        raise errors.MalformedOutputError(f"model output failed schema validation: {e}") from e


class ClaudeClient:
    """Shared request/response handling for Claude: one JSON-constrained call, errors mapped to our types."""

    def __init__(self, model: str = DEFAULT_MODEL, effort: str = "medium", client=None):
        import anthropic

        self._anthropic = anthropic
        self.model = model
        self.effort = effort
        self.client = client or anthropic.Anthropic()

    def generate_json(self, system: str, content: list[dict], schema: dict) -> tuple[str, str]:
        """Returns (output text, model that served it)."""
        a = self._anthropic
        try:
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",  # if the model declines, the API retries on Anthropic's recommended fallback
                system=system,
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
                messages=[{"role": "user", "content": content}],
            )
        except (a.AuthenticationError, a.PermissionDeniedError) as e:
            raise errors.AuthError(f"API authentication failed: {e.message}") from e
        except a.RateLimitError as e:
            ra = e.response.headers.get("retry-after")
            raise errors.RateLimitError(f"rate limited: {e.message}", float(ra) if ra else None) from e
        except a.BadRequestError as e:
            raise errors.PermanentAPIError(f"request rejected: {e.message}") from e
        except a.APIStatusError as e:
            if e.status_code >= 500:
                raise errors.TransientAPIError(f"server error {e.status_code}: {e.message}") from e
            raise errors.PermanentAPIError(f"API error {e.status_code}: {e.message}") from e
        except a.APIConnectionError as e:  # includes timeouts
            raise errors.TransientAPIError(f"connection error: {e}") from e

        if resp.stop_reason == "refusal":
            cat = resp.stop_details.category if resp.stop_details else None
            raise errors.RefusalError(f"model declined (category={cat})")
        if resp.stop_reason == "max_tokens":
            raise errors.MalformedOutputError("output truncated at max_tokens")
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if text is None:
            raise errors.MalformedOutputError("no text block in response")
        return text, resp.model


class ClaudeExtractor(ClaudeClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.schema = strict_json_schema(ExtractedReceipt)

    def extract(self, image: PreparedImage) -> ExtractionResult:
        data = base64.standard_b64encode(image.jpeg_bytes).decode("ascii")
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
            {"type": "text", "text": USER_PROMPT},
        ]
        text, model = self.generate_json(SYSTEM_PROMPT, content, self.schema)
        return ExtractionResult(parse_output(text), text, model, PROMPT_VERSION)


def save_fixture(result: ExtractionResult, sha256: str, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{sha256}.json"
    path.write_text(json.dumps({"model": result.model, "prompt_version": result.prompt_version,
                                "output": json.loads(result.raw_json)}, indent=2), encoding="utf-8")
    return path


class ReplayExtractor:
    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def extract(self, image: PreparedImage) -> ExtractionResult:
        path = self.directory / f"{image.original_sha256}.json"
        if not path.exists():
            raise errors.PermanentAPIError(f"no saved extraction for this image ({path.name})")
        saved = json.loads(path.read_text(encoding="utf-8"))
        raw = json.dumps(saved["output"])
        return ExtractionResult(parse_output(raw), raw, saved["model"], saved["prompt_version"])


class FakeExtractor:
    """Maps image sha256 -> ExtractedReceipt, an Exception to raise, or a list of those (one per call,
    e.g. [TransientAPIError(...), receipt] = fail once, then succeed). Thread-safe."""

    def __init__(self, responses: dict, model: str = "fake"):
        import threading
        self.responses = {k: list(v) if isinstance(v, list) else v for k, v in responses.items()}
        self.model = model
        self.calls = 0
        self._lock = threading.Lock()

    def extract(self, image: PreparedImage) -> ExtractionResult:
        with self._lock:
            self.calls += 1
            r = self.responses[image.original_sha256]
            if isinstance(r, list):
                r = r.pop(0) if len(r) > 1 else r[0]
        if isinstance(r, Exception):
            raise r
        return ExtractionResult(r, r.model_dump_json(), self.model, "fake")
