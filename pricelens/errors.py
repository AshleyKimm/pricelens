"""Distinct error types so callers (and later the batch runner) can decide: retry, fail fast, or flag.

A totals mismatch is deliberately NOT an exception: the receipt is stored with
status='needs_review' so the work isn't lost.
"""


class PriceLensError(Exception):
    """Base class. `retryable` tells the (Layer 4) retry policy whether to try again."""

    retryable = False


class UnreadableImageError(PriceLensError):
    """File missing, not an image, or corrupt."""


class RedactionUnavailableError(PriceLensError):
    """Local OCR (Tesseract) isn't installed, so we refuse to send an unredacted image."""


class AuthError(PriceLensError):
    """Missing or invalid API credentials. Never retried."""


class RateLimitError(PriceLensError):
    retryable = True

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class TransientAPIError(PriceLensError):
    """Timeouts, connection errors, 5xx/overloaded."""

    retryable = True


class PermanentAPIError(PriceLensError):
    """400-class errors other than auth/rate limit (e.g. image rejected)."""


class RefusalError(PriceLensError):
    """The model declined the request (stop_reason == 'refusal')."""


class MalformedOutputError(PriceLensError):
    """Model output did not validate against the extraction schema."""


class DuplicateReceiptError(PriceLensError):
    """This exact image (same sha256) is already stored."""

    def __init__(self, receipt_id: int):
        super().__init__(f"image already stored as receipt #{receipt_id}")
        self.receipt_id = receipt_id
