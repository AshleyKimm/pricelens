import json
from pathlib import Path

import pytest
from helpers import line, receipt
from PIL import Image

from pricelens import db, errors
from pricelens.gemini import GeminiExtractor
from pricelens.pipeline import add_receipt
from pricelens.policy import DataPolicyError, check_allowed, is_public_data
from pricelens.redact import PreparedImage, prepare

IMG = PreparedImage("abc", b"\xff\xd8fakejpeg", [], 0)
GOOD = receipt([line("MILK 3.49", "3.49")], subtotal="3.49", total="3.49")


def ok_body(text, status="completed", model="gemini-3.6-flash"):
    return json.dumps({"model": model, "status": status,
                       "steps": [{"type": "model_output", "content": [{"type": "text", "text": text}]}]}).encode()


def fake(status=200, body=b"", headers=None, seen=None):
    def transport(url, hdrs, payload):
        if seen is not None:
            seen.update(url=url, headers=hdrs, body=json.loads(payload))
        return status, headers or {}, body
    return transport


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")


def test_request_shape_and_parsed_result():
    seen = {}
    ex = GeminiExtractor(transport=fake(body=ok_body(GOOD.model_dump_json()), seen=seen))
    res = ex.extract(IMG)
    assert res.receipt.total == "3.49" and res.model == "gemini-3.6-flash"
    assert seen["headers"]["x-goog-api-key"] == "test-key"
    body = seen["body"]
    assert body["input"][0]["type"] == "image" and body["input"][0]["mime_type"] == "image/jpeg"
    assert body["response_format"]["mime_type"] == "application/json"
    assert body["store"] is False


@pytest.mark.parametrize("status,exc", [
    (401, errors.AuthError), (403, errors.AuthError), (429, errors.RateLimitError),
    (500, errors.TransientAPIError), (503, errors.TransientAPIError), (400, errors.PermanentAPIError),
])
def test_http_errors_map_to_distinct_types(status, exc):
    body = json.dumps({"error": {"code": status, "status": "X", "message": "nope"}}).encode()
    with pytest.raises(exc):
        GeminiExtractor(transport=fake(status, body)).extract(IMG)


def test_retry_after_is_passed_on():
    with pytest.raises(errors.RateLimitError) as e:
        GeminiExtractor(transport=fake(429, b"{}", {"Retry-After": "7"})).extract(IMG)
    assert e.value.retry_after == 7.0 and e.value.retryable


def test_malformed_output_is_rejected_before_storage():
    with pytest.raises(errors.MalformedOutputError):
        GeminiExtractor(transport=fake(body=ok_body('{"store_name": "X"}'))).extract(IMG)


def test_incomplete_and_failed_status():
    with pytest.raises(errors.MalformedOutputError):
        GeminiExtractor(transport=fake(body=ok_body("{", status="incomplete"))).extract(IMG)
    with pytest.raises(errors.RefusalError):
        GeminiExtractor(transport=fake(body=ok_body("", status="failed"))).extract(IMG)


def test_missing_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY")
    with pytest.raises(errors.AuthError):
        GeminiExtractor(transport=fake()).extract(IMG)


def test_free_tier_is_the_default(monkeypatch):
    monkeypatch.delenv("GEMINI_TIER", raising=False)
    assert GeminiExtractor().trains_on_input
    assert not GeminiExtractor(tier="paid").trains_on_input
    assert GeminiExtractor(tier="typo").trains_on_input  # anything unclear is treated as free


def test_public_data_paths(tmp_path):
    assert is_public_data(tmp_path / "evals/data/cord/x.png", tmp_path)
    assert is_public_data(tmp_path / "demo/receipts/x.png", tmp_path)
    assert not is_public_data(tmp_path / "my_receipts/x.png", tmp_path)
    assert not is_public_data(tmp_path / "evals/data/cord/../../../x.png", tmp_path)  # no escaping


def test_free_tier_refuses_private_receipt_before_any_processing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "my_receipt.png"
    Image.new("RGB", (50, 50), "white").save(p)
    seen = {}
    ex = GeminiExtractor(transport=fake(body=ok_body(GOOD.model_dump_json()), seen=seen))
    with pytest.raises(DataPolicyError):
        add_receipt(p, conn=db.connect(":memory:"), extractor=ex, redacted_dir=tmp_path / "red",
                    prepare_fn=lambda q: prepare(q, ocr=lambda im: []))
    assert seen == {} and not (tmp_path / "red").exists()


def test_no_training_extractors_are_unrestricted(tmp_path):
    class Plain:  # e.g. Claude, Replay, Fake: no trains_on_input attribute
        pass
    check_allowed(Plain(), tmp_path / "anything.png", tmp_path)
