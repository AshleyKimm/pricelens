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
