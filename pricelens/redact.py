"""Local preprocessing + redaction. Runs BEFORE anything is sent to the API.

Pipeline: open -> fix orientation -> OCR locally (Tesseract) -> black out sensitive words ->
downscale -> re-encode as JPEG (drops EXIF/GPS) -> these exact bytes are previewed and sent.

Rules (find_sensitive_boxes, unit-tested without Tesseract):
  R1 masked card numbers: words like XXXX1234, ****1234, and the digits after them
  R2 runs of adjacent digit/mask words totalling >= 8 characters (card/account numbers split by OCR)
  R3 on lines labelled CARD/ACCT/MEMBER/LOYALTY/REWARDS/AUTH/APPROVAL/REF: any word with >= 4
     digits that is not a money amount (so "MEMBER SAVINGS -1.00" keeps its price)
R2 also blacks out 8+ digit item codes/UPCs; that costs nothing because we don't use them.
OCR can miss text on crumpled receipts, which is why the CLI requires a human look at the preview.
"""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps, UnidentifiedImageError

from .errors import RedactionUnavailableError, UnreadableImageError

MAX_LONG_EDGE = 2000  # px; tall receipts need height to stay legible. Tune with the eval.
JPEG_QUALITY = 88
PAD = 4

_MASKED = re.compile(r"^[X*#•]{3,}[-\s]?\d{0,4}$", re.I)
_DIGITISH = re.compile(r"^[\dX*#•-]+$", re.I)
_MONEY = re.compile(r"^-?\$?\d+\.\d{2}-?$")
_CONTEXT = re.compile(r"\b(CARD|ACCT|ACCOUNT|MEMBER|LOYALTY|REWARDS?|AUTH|APPROVAL|APPR|REF)\b", re.I)


@dataclass(frozen=True)
class Word:
    text: str
    left: int
    top: int
    width: int
    height: int
    line_key: tuple  # (block, paragraph, line) from Tesseract


@dataclass(frozen=True)
class Box:
    left: int
    top: int
    right: int
    bottom: int


def _box(w: Word) -> Box:
    return Box(w.left - PAD, w.top - PAD, w.left + w.width + PAD, w.top + w.height + PAD)


def find_sensitive_boxes(words: list[Word]) -> list[Box]:
    lines: dict[tuple, list[Word]] = {}
    for w in words:
        if w.text.strip():
            lines.setdefault(w.line_key, []).append(w)

    hits: list[Word] = []
    for line in lines.values():
        line.sort(key=lambda w: w.left)
        text = " ".join(w.text for w in line)

        # R1: masked card number and the 4 digits that may follow as a separate word
        for i, w in enumerate(line):
            if _MASKED.match(w.text):
                hits.append(w)
                if i + 1 < len(line) and re.fullmatch(r"\d{4}", line[i + 1].text):
                    hits.append(line[i + 1])

        # R2: runs of adjacent digit-ish words with >= 8 characters in total
        run: list[Word] = []
        for w in line + [Word("", 0, 0, 0, 0, ())]:  # sentinel flushes the last run
            # a word joins the run if it has a digit, or is a mask like XXXX (a lone '#' or 'X' doesn't)
            if w.text and _DIGITISH.match(w.text) and (any(c.isdigit() for c in w.text) or len(w.text) >= 3):
                run.append(w)
                continue
            if sum(len(x.text.replace("-", "")) for x in run) >= 8:
                hits.extend(run)
            run = []

        # R3: context lines
        if _CONTEXT.search(text):
            for w in line:
                if sum(c.isdigit() for c in w.text) >= 4 and not _MONEY.match(w.text):
                    hits.append(w)

    seen, out = set(), []
    for w in hits:
        k = (w.left, w.top, w.width, w.height)
        if k not in seen:
            seen.add(k)
            out.append(_box(w))
    return out


def _ocr_words_denoised(img: Image.Image) -> list[Word]:
    """Second OCR pass for speckled/noisy photos: a 3x3 median filter removes salt-and-pepper noise that
    stops Tesseract from reading even large, clear words like TOTAL."""
    return _ocr_words(img.filter(ImageFilter.MedianFilter(3)))


def _ocr_words(img: Image.Image) -> list[Word]:
    try:
        import pytesseract
        from pytesseract import Output
    except ImportError as e:
        raise RedactionUnavailableError("pytesseract is not installed") from e
    import os
    import shutil
    cmd = os.environ.get("TESSERACT_CMD")
    default_win = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    elif not shutil.which("tesseract") and default_win.exists():
        pytesseract.pytesseract.tesseract_cmd = str(default_win)
    # OCR works better on larger grayscale images; map boxes back afterwards.
    scale = 2 if img.width < 1200 else 1
    gray = ImageOps.grayscale(img)
    if scale != 1:
        gray = gray.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    try:
        d = pytesseract.image_to_data(gray, output_type=Output.DICT, config="--psm 6")
    except pytesseract.TesseractNotFoundError as e:
        raise RedactionUnavailableError(
            "Tesseract OCR is not installed or not on PATH; refusing to send an unredacted image. "
            "Install it, or set TESSERACT_CMD to tesseract.exe."
        ) from e
    return [
        Word(d["text"][i], d["left"][i] // scale, d["top"][i] // scale,
             -(-d["width"][i] // scale), -(-d["height"][i] // scale),
             (d["block_num"][i], d["par_num"][i], d["line_num"][i]))
        for i in range(len(d["text"]))
        if d["text"][i].strip()
    ]


_TOTAL = re.compile(r"^(GRAND\s+)?TOTAL\b(?!\s*(SAVINGS|SAVED|ITEMS|QTY|DISCOUNT))|^(BALANCE|AMOUNT)\s+DUE\b", re.I)
_SUBTOTAL = re.compile(r"\bSUB\s*-?\s*TOTAL\b", re.I)


def find_total_bottom(words: list[Word]) -> int | None:
    """y just below the receipt's TOTAL line, or None if OCR didn't find one.

    Payment, card, auth, change and loyalty lines are printed below TOTAL, and nothing below it is needed for
    extraction (tax and after-subtotal adjustments come before TOTAL). Finding the word TOTAL is much easier
    for OCR than reading a blurry card number, so blacking out everything below it protects the numbers OCR
    can't read. Uses the first TOTAL after SUBTOTAL (so 'TOTAL SAVINGS' further down doesn't count).
    """
    lines: dict[tuple, list[Word]] = {}
    for w in words:
        lines.setdefault(w.line_key, []).append(w)
    ordered = sorted(lines.values(), key=lambda ws: min(w.top for w in ws))
    texts = [" ".join(w.text for w in sorted(ws, key=lambda w: w.left)) for ws in ordered]
    start = next((i + 1 for i, t in enumerate(texts) if _SUBTOTAL.search(t)), 0)
    for ws, text in zip(ordered[start:], texts[start:]):
        if _TOTAL.search(text.strip()) and not _SUBTOTAL.search(text):
            if not any(re.search(r"\d+[.,]\d{2}", t) for t in texts[:ordered.index(ws)]):
                return None  # a "TOTAL" with no prices above it is probably a misread header
            return max(w.top + w.height for w in ws) + PAD
    return None


@dataclass
class PreparedImage:
    original_sha256: str
    jpeg_bytes: bytes          # exactly what gets sent to the API
    boxes: list[Box]           # redaction boxes, in original-image coordinates
    ocr_word_count: int
    blacked_below: int | None = None   # y from which everything was blacked out (below TOTAL), if found

    @property
    def redaction_confident(self) -> bool:
        """True only if the TOTAL line was located and everything below it blacked out. Otherwise OCR may
        have missed sensitive numbers, and the image must not be sent without a human looking at it."""
        # Boxes alone don't count: on a noisy test receipt OCR boxed the auth code but missed the card digits
        # and member number on the lines around it. Only a located TOTAL proves the whole section is covered.
        return self.blacked_below is not None


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_image(path: Path) -> Image.Image:
    try:
        img = Image.open(path)
        img.load()
    except (FileNotFoundError, IsADirectoryError) as e:
        raise UnreadableImageError(f"no such image: {path}") from e
    except (UnidentifiedImageError, OSError) as e:
        raise UnreadableImageError(f"not a readable image: {path} ({e})") from e
    return ImageOps.exif_transpose(img).convert("RGB")


def prepare(path: Path, ocr=None) -> PreparedImage:
    """Orient, redact, downscale, re-encode. `ocr` is injectable for tests (then no second pass runs)."""
    path = Path(path)
    sha = sha256_file(path) if path.exists() else None
    img = load_image(path)
    words = (ocr or _ocr_words)(img)
    below = find_total_bottom(words)
    if below is None and ocr is None:
        # Noisy photo? Try once more on a denoised copy, and keep the boxes from both passes.
        second = _ocr_words_denoised(img)
        below = find_total_bottom(second)
        # Each pass numbers its lines independently: tag pass 2's line keys so its words are never grouped
        # with a different physical line from pass 1 (that merged "2026" from the date into a fake long number).
        words = words + [Word(w.text, w.left, w.top, w.width, w.height, ("pass2",) + tuple(w.line_key))
                         for w in second]
    boxes = find_sensitive_boxes(words)
    draw = ImageDraw.Draw(img)
    for b in boxes:
        draw.rectangle([b.left, b.top, b.right, b.bottom], fill="black")
    if below is not None and below < img.height:
        draw.rectangle([0, below, img.width, img.height], fill="black")  # layer 1: everything under TOTAL
    if max(img.size) > MAX_LONG_EDGE:
        img.thumbnail((MAX_LONG_EDGE, MAX_LONG_EDGE), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)  # new encode: no EXIF/GPS carried over
    return PreparedImage(sha, buf.getvalue(), boxes, len(words), below)
