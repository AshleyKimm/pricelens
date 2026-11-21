import io

import pytest
from PIL import Image

from pricelens.errors import UnreadableImageError
from pricelens.redact import Word, find_sensitive_boxes, prepare


def words_on_line(*texts, line=1):
    x, out = 0, []
    for t in texts:
        out.append(Word(t, x, line * 20, 10 * len(t), 12, (1, 1, line)))
        x += 10 * len(t) + 8
    return out


def redacted(words):
    boxes = find_sensitive_boxes(words)
    return {w.text for w in words if any(b.left == w.left - 4 and b.top == w.top - 4 for b in boxes)}


def test_masked_card_and_last_four():
    assert redacted(words_on_line("VISA", "XXXX", "XXXX", "XXXX", "1234", "12.30")) == {"XXXX", "1234"}
    assert redacted(words_on_line("CARD", "****1234")) == {"****1234"}


def test_split_card_number():
    assert redacted(words_on_line("4111", "1111", "1111", "1111")) == {"4111", "1111"}


def test_loyalty_line_keeps_money():
    got = redacted(words_on_line("MEMBER", "#", "88123456", "SAVINGS", "-1.00"))
    assert got == {"88123456"}


def test_auth_code_on_context_line():
    assert redacted(words_on_line("AUTH", "CODE", "004512")) == {"004512"}


def test_item_lines_untouched():
    assert redacted(words_on_line("2", "@", "1.50", "YOGURT", "3.00")) == set()
    assert redacted(words_on_line("BANANAS", "1.32", "lb", "0.78")) == set()
    assert redacted(words_on_line("11/18/2026", "14:02")) == set()


def test_lines_are_independent():
    words = words_on_line("CARD", "XXXX1234", line=1) + words_on_line("MILK", "3.49", line=2)
    assert redacted(words) == {"XXXX1234"}


def _png(tmp_path, name="r.png"):
    p = tmp_path / name
    Image.new("RGB", (300, 600), "white").save(p)
    return p


def test_prepare_blacks_out_boxes_and_strips_exif(tmp_path):
    src = tmp_path / "r.jpg"
    img = Image.new("RGB", (300, 600), "white")
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"  # Make tag
    img.save(src, exif=exif)

    fake_ocr = lambda im: words_on_line("CARD", "XXXX1234")
    out = prepare(src, ocr=fake_ocr)
    sent = Image.open(io.BytesIO(out.jpeg_bytes))
    assert len(out.boxes) == 1
    b = out.boxes[0]
    assert sent.getpixel(((b.left + b.right) // 2, (b.top + b.bottom) // 2))[0] < 40  # blacked out
    assert len(sent.getexif()) == 0


def test_prepare_downscales_tall_images(tmp_path):
    p = tmp_path / "tall.png"
    Image.new("RGB", (800, 4000), "white").save(p)
    sent = Image.open(io.BytesIO(prepare(p, ocr=lambda im: []).jpeg_bytes))
    assert max(sent.size) == 2000


def test_unreadable_image(tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    with pytest.raises(UnreadableImageError):
        prepare(bad, ocr=lambda im: [])


# --- layer 1: black out everything below TOTAL ---------------------------------------------------

from pricelens.redact import find_total_bottom  # noqa: E402


def receipt_words(*rows):
    out = []
    for i, row in enumerate(rows, start=1):
        out += words_on_line(*row.split(), line=i)
    return out


def test_total_after_subtotal_is_found():
    w = receipt_words("MILK 3.49", "SUBTOTAL 3.49", "TAX 0.00", "TOTAL 3.49", "VISA XXXX 1234", "TOTAL SAVINGS 0.50")
    assert find_total_bottom(w) == 4 * 20 + 12 + 4  # bottom of the 4th line + padding


def test_total_savings_and_subtotal_are_not_the_total():
    assert find_total_bottom(receipt_words("MILK 3.49", "SUBTOTAL 3.49", "TOTAL SAVINGS 0.50")) is None
    assert find_total_bottom(receipt_words("MILK 3.49", "BALANCE DUE 3.49")) is not None


def test_total_without_prices_above_is_ignored():
    assert find_total_bottom(receipt_words("TOTAL WINE AND MORE", "MILK 3.49")) is None


def test_everything_below_total_is_blacked_out(tmp_path):
    p = tmp_path / "r.png"
    Image.new("RGB", (300, 300), "white").save(p)
    w = receipt_words("MILK 3.49", "TOTAL 3.49")
    out = prepare(p, ocr=lambda im: w)
    img = Image.open(io.BytesIO(out.jpeg_bytes))
    assert out.blacked_below == 2 * 20 + 12 + 4 and out.redaction_confident
    assert img.getpixel((150, 250))[0] < 40   # payment area: black
    assert img.getpixel((150, 10))[0] > 200   # item area: untouched


# --- layer 2: the gate ---------------------------------------------------------------------------

import pytest as _pytest  # noqa: E402

from pricelens.errors import RedactionUncertainError  # noqa: E402
from pricelens.extract import FakeExtractor  # noqa: E402
from pricelens.pipeline import process  # noqa: E402
from pricelens.redact import sha256_file  # noqa: E402


def _img(tmp_path, sub="mine"):
    d = tmp_path / sub
    d.mkdir(parents=True, exist_ok=True)
    p = d / "r.png"
    Image.new("RGB", (60, 60), "white").save(p)
    return p


def test_uncertain_redaction_is_never_sent_automatically(tmp_path):
    p = _img(tmp_path)
    fx = FakeExtractor({})
    with _pytest.raises(RedactionUncertainError):
        process(p, sha=sha256_file(p), extractor=fx, redacted_dir=tmp_path / "red", prepare_fn=lambda q: prepare(q, ocr=lambda im: []))
    assert fx.calls == 0


def test_override_only_works_for_public_data(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    private = _img(tmp_path, "mine")
    with _pytest.raises(RedactionUncertainError):  # a real receipt can't skip the gate
        process(private, sha="a", extractor=FakeExtractor({}), redacted_dir=tmp_path / "red",
                prepare_fn=lambda q: prepare(q, ocr=lambda im: []), allow_uncertain_redaction=True)
    public = _img(tmp_path, "evals/synthetic")
    from helpers import receipt
    fx = FakeExtractor({sha256_file(public): receipt([])})
    process(public, sha=sha256_file(public), extractor=fx, redacted_dir=tmp_path / "red",
            prepare_fn=lambda q: prepare(q, ocr=lambda im: []), allow_uncertain_redaction=True)
    assert fx.calls == 1


def test_interactive_confirm_still_gets_the_choice(tmp_path):
    p = _img(tmp_path)
    seen = []
    from pricelens.pipeline import Cancelled
    with _pytest.raises(Cancelled):
        process(p, sha=sha256_file(p), extractor=FakeExtractor({}), redacted_dir=tmp_path / "red",
                prepare_fn=lambda q: prepare(q, ocr=lambda im: []),
                confirm=lambda prepared, path: seen.append(prepared.redaction_confident) or False)
    assert seen == [False]  # the prompt is told redaction is uncertain


def test_batch_records_uncertain_images_for_review_not_retry(tmp_path):
    from pricelens import db
    from pricelens.batch import failed_paths, run_batch
    from pricelens.reliability import Caller
    p = _img(tmp_path)
    conn = db.connect(":memory:")
    s = run_batch(conn, [p], extractor=FakeExtractor({}), caller=Caller(), redacted_dir=tmp_path / "red",
                  prepare_fn=lambda q: prepare(q, ocr=lambda im: []))
    assert s.failed == 1 and s.failures[0][1] == "RedactionUncertainError"
    assert failed_paths(conn) == []  # needs a human, not a retry


def test_noisy_photo_gets_a_second_denoised_pass_with_separate_lines(tmp_path, monkeypatch):
    """Regression: pass 1 misses TOTAL; pass 2 finds it. Both passes number lines from 1, and merging them by
    line number once glued the date's '2026' to digits from another line, so it was blacked out."""
    import pricelens.redact as R
    p = tmp_path / "r.png"
    Image.new("RGB", (300, 300), "white").save(p)
    first = words_on_line("OCT", "26,", "2026", line=1) + words_on_line("MILK", "3.49", line=2) \
        + words_on_line("T0TAL", "3.49", line=3)                        # misread: no TOTAL found
    second = words_on_line("1234", "5678", line=1)                      # pass 2's line 1 is the card line...
    second = [R.Word(w.text, w.left, 200, w.width, w.height, w.line_key) for w in second]  # ...physically low
    second += [R.Word("MILK", 0, 40, 40, 12, (1, 1, 2)), R.Word("3.49", 50, 40, 40, 12, (1, 1, 2)),
               R.Word("TOTAL", 0, 60, 50, 12, (1, 1, 3)), R.Word("3.49", 60, 60, 40, 12, (1, 1, 3))]
    monkeypatch.setattr(R, "_ocr_words", lambda im: first)
    monkeypatch.setattr(R, "_ocr_words_denoised", lambda im: second)
    out = prepare(p)
    assert out.redaction_confident                                       # found by the second pass
    date_word = first[2]
    assert not any(b.left <= date_word.left <= b.right and b.top <= date_word.top <= b.bottom for b in out.boxes)


def test_boxes_alone_are_not_enough(tmp_path):
    """Regression: OCR boxed one payment line but missed the card digits next to it; that must not count as safe."""
    p = tmp_path / "r.png"
    Image.new("RGB", (300, 300), "white").save(p)
    out = prepare(p, ocr=lambda im: words_on_line("AUTH", "CODE", "004512"))
    assert out.boxes and not out.redaction_confident
