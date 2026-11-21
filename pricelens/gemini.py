"""Gemini extractor over the REST Interactions API, using only the standard library.

Why stdlib urllib instead of the SDK: no new dependency, and raw HTTP status codes map
directly onto our error classes (which the Layer 4 retry policy needs).

PRIVACY: on the free tier Google may use inputs to improve its products and human reviewers
may read them. This extractor sets `trains_on_input = True`, and the pipeline then refuses
any image that isn't in a public-data folder (see policy.py).
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from typing import Callable

from . import errors
from .extract import SYSTEM_PROMPT, USER_PROMPT, ExtractionResult, PROMPT_VERSION, parse_output
from .models import ExtractedReceipt, strict_json_schema
from .redact import PreparedImage

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"
DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"

# transport(url, headers, body_bytes) -> (status, headers, body_bytes); injectable for tests.
Transport = Callable[[str, dict, bytes], tuple[int, dict, bytes]]


def urllib_transport(url: str, headers: dict, body: bytes, timeout: float = 120) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise errors.TransientAPIError(f"connection error: {e}") from e


def _api_key() -> str:
    key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")  # Google's documented precedence
    if not key:
        raise errors.AuthError("GEMINI_API_KEY is not set (put it in .env)")
    return key


def _error_message(body: bytes) -> str:
    try:
        err = json.loads(body).get("error", {})
        return f"{err.get('status') or err.get('code')}: {err.get('message')}"
    except (ValueError, AttributeError):
        return body[:200].decode("utf-8", "replace")


class GeminiExtractor:
    def __init__(self, model: str = DEFAULT_GEMINI_MODEL, transport: Transport = urllib_transport,
                 tier: str | None = None):
        # Read at construction (after .env is loaded), not import. Anything but "paid" is treated as free.
        tier = (tier or os.environ.get("GEMINI_TIER", "free")).lower()
        self.trains_on_input = tier != "paid"
        self.model = model
        self.transport = transport
        self.schema = strict_json_schema(ExtractedReceipt)

    def request_body(self, image: PreparedImage) -> dict:
        return {
            "model": self.model,
            "system_instruction": SYSTEM_PROMPT,
            "input": [
                {"type": "image", "data": base64.standard_b64encode(image.jpeg_bytes).decode("ascii"),
                 "mime_type": "image/jpeg"},
                {"type": "text", "text": USER_PROMPT},
            ],
            "response_format": {"type": "text", "mime_type": "application/json", "schema": self.schema},
            "store": False,  # don't keep the interaction server-side; we never continue it
        }

    def extract(self, image: PreparedImage) -> ExtractionResult:
        headers = {"x-goog-api-key": _api_key(), "Content-Type": "application/json"}
        status, resp_headers, body = self.transport(ENDPOINT, headers, json.dumps(self.request_body(image)).encode())

        if status in (401, 403):
            raise errors.AuthError(f"Gemini rejected the API key ({status}) {_error_message(body)}")
        if status == 429:
            ra = resp_headers.get("Retry-After") or resp_headers.get("retry-after")
            raise errors.RateLimitError(f"Gemini rate limit/quota: {_error_message(body)}",
                                        float(ra) if ra else None)
        if status >= 500 or status == 408:
            raise errors.TransientAPIError(f"Gemini server error {status}: {_error_message(body)}")
        if status >= 400:
            raise errors.PermanentAPIError(f"Gemini rejected the request ({status}) {_error_message(body)}")

        try:
            data = json.loads(body)
        except ValueError as e:
            raise errors.MalformedOutputError("response was not JSON") from e
        if data.get("errors"):
            raise errors.PermanentAPIError(f"Gemini interaction error: {data['errors']}")
        state = data.get("status")
        if state == "incomplete":
            raise errors.MalformedOutputError("output incomplete (likely hit the output token limit)")
        if state != "completed":
            raise errors.RefusalError(f"interaction ended with status {state!r}")

        texts = [c.get("text", "") for step in data.get("steps", []) if step.get("type") == "model_output"
                 for c in step.get("content", []) if c.get("type") == "text"]
        if not texts:
            raise errors.MalformedOutputError("no text output in response")
        text = "".join(texts)
        return ExtractionResult(parse_output(text), text, data.get("model", self.model), PROMPT_VERSION)
