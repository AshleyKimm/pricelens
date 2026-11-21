"""Generate fake receipts + ground-truth labels (deterministic: same seed -> same files).

Usage:  python evals/synthetic/generate.py [--seed 7]
Writes: evals/synthetic/receipts/<id>.png and evals/synthetic/labels/<id>.json

All stores, prices, card and loyalty numbers are invented. The labels are the answer key for
the eval (extraction AND Layer 2 categories) and the source of the public demo.
"""
from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).parent

# normalized_name, category, base price (cents, per unit), unit ('each' or 'lb'), printed names per store
CATALOG = [
    ("bananas", "produce", 59, "lb", ("BANANAS", "BNNA ORG", "ORG BANANAS")),
    ("gala apples", "produce", 199, "lb", ("GALA APPLES", "APPLE GALA", "GALA APL")),
    ("avocado", "produce", 125, "each", ("AVOCADO HASS", "AVOCADO", "HASS AVO")),
    ("milk", "dairy_eggs", 349, "each", ("MILK 1GAL", "WHOLE MILK GAL", "MLK WHL 1G")),
    ("eggs", "dairy_eggs", 429, "each", ("EGGS DOZEN", "LG EGGS 12CT", "EGGS GRD A 12")),
    ("greek yogurt", "dairy_eggs", 150, "each", ("GREEK YOGURT", "YOGURT GRK", "YGRT GRK PLN")),
    ("cheddar cheese", "dairy_eggs", 499, "each", ("CHEDDAR 8OZ", "SHRP CHED 8OZ", "CHEDDAR BLK")),
    ("sourdough bread", "bakery", 449, "each", ("SOURDOUGH LOAF", "BREAD SRDGH", "SRDO BREAD")),
    ("flour tortillas", "bakery", 299, "each", ("TORTILLAS FLOUR", "FLR TORTILLA 10", "TORT FLOUR")),
    ("chicken breast", "meat_seafood", 399, "lb", ("CHKN BREAST", "CHICKEN BRST BNLS", "BNLS CHIX BRST")),
    ("ground beef", "meat_seafood", 549, "lb", ("GROUND BEEF 80/20", "GRND BEEF", "GR BF 80%")),
    ("spaghetti", "pantry", 179, "each", ("SPAGHETTI 1LB", "PASTA SPAGH", "SPAG 16OZ")),
    ("rice", "pantry", 399, "each", ("JASMINE RICE 2LB", "RICE JASMN", "JSMN RICE")),
    ("olive oil", "pantry", 899, "each", ("OLIVE OIL EV 500ML", "EVOO 16.9OZ", "OLV OIL XV")),
    ("peanut butter", "pantry", 349, "each", ("PEANUT BUTTER", "PNT BTR CRMY", "PB CREAMY")),
    ("coffee", "beverages", 899, "each", ("COFFEE 12OZ", "COFFEE GRND MED", "CFE GRD 12Z")),
    ("orange juice", "beverages", 449, "each", ("ORANGE JUICE 52OZ", "OJ NO PULP", "ORG JCE 52")),
    ("sparkling water", "beverages", 549, "each", ("SPARKLING WATER 12PK", "SELTZER 12PK", "SPRK WTR 12")),
    ("tortilla chips", "snacks_sweets", 399, "each", ("TORTILLA CHIPS", "CHIPS TORT", "TRT CHIP")),
    ("dark chocolate", "snacks_sweets", 299, "each", ("DARK CHOC BAR", "CHOC DRK 72%", "DK CHOC")),
    ("frozen pizza", "frozen", 699, "each", ("FROZEN PIZZA", "PIZZA FRZ PEPP", "FZ PIZZA")),
    ("ice cream", "frozen", 549, "each", ("ICE CREAM VAN 1.5QT", "ICE CRM VAN", "IC VANILLA")),
    ("paper towels", "household", 1299, "each", ("PAPER TOWELS 6RL", "PPR TWL 6", "PAPER TWL")),
    ("dish soap", "household", 349, "each", ("DISH SOAP", "DISH LIQ", "DSH SOAP")),
    ("toothpaste", "personal_care", 399, "each", ("TOOTHPASTE", "TTHPST MINT", "TP MINT")),
    ("dog food", "baby_pet", 1599, "each", ("DOG FOOD 5LB", "DOG FD CHKN", "DG FOOD")),
]
TAXABLE = {"household", "personal_care", "baby_pet", "snacks_sweets"}


@dataclass
class Store:
    idx: int
    name: str
    header: tuple[str, ...]
    price_factor: float
    tax_rate: Decimal
    date_style: str


STORES = [
    Store(0, "FRESHWAY GROCERY", ("FRESHWAY GROCERY", "123 MAIN ST"), 1.00, Decimal("0.07"), "us_long"),
    Store(1, "CORNER MARKET", ("CORNER MARKET", "45 ELM AVE"), 1.08, Decimal("0.0625"), "us_short"),
    Store(2, "MAPLE LANE FOODS", ("MAPLE LANE FOODS", "9 MAPLE LN"), 0.95, Decimal("0.08"), "month_name"),
]


def r(amount: Decimal) -> int:
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def money(cents: int) -> str:
    return f"{abs(cents) // 100}.{abs(cents) % 100:02d}" + ("-" if cents < 0 else "")


def fmt_date(d: date, style: str, hour: int, minute: int) -> str:
    if style == "us_long":
        return f"{d:%m/%d/%Y} {hour:02d}:{minute:02d}"
    if style == "us_short":
        return f"{d:%m/%d/%y} {hour:02d}:{minute:02d}"
    h12 = hour % 12 or 12
    return f"{d:%b} {d.day}, {d.year} {h12}:{minute:02d}{'PM' if hour >= 12 else 'AM'}".upper()


@dataclass
class Line:
    printed: list[str]       # 1 or 2 printed text rows (name row, optional detail row)
    amount: int              # printed price column value, signed
    kind: str
    section: str
    quantity: float | None = None
    unit: str | None = None
    unit_price: int | None = None
    normalized_name: str | None = None
    category: str | None = None
    applies_to: int | None = None
    pack_count: int | None = None  # items per package (eggs), for pack-size tests


@dataclass
class Receipt:
    id: str
    store: Store
    day: date
    date_raw: str
    lines: list[Line] = field(default_factory=list)
    print_subtotal: bool = True
    misprint: str | None = None
    degradation: str = "clean"


def price_on(store: Store, product: tuple, week: int, rng_seed: int) -> int:
    """Store-specific price with deterministic changes over time (so price history has ups and downs)."""
    name = product[0]
    rng = random.Random(f"{rng_seed}-{store.idx}-{name}")
    base = Decimal(product[2]) * Decimal(str(store.price_factor))
    change_week = rng.randint(1, 5)
    step = Decimal(rng.choice(["0.90", "1.00", "1.10", "1.15", "0.95"]))
    factor = step if week >= change_week else Decimal(1)
    cents = r(base * factor)
    return max(9, cents - cents % 10 + 9) if product[3] == "each" else cents  # x.x9 shelf prices


def build_receipts(seed: int) -> list[Receipt]:
    rng = random.Random(seed)
    start = date(2026, 10, 1)
    receipts = []
    for week in range(6):
        for store in STORES:
            day = start + timedelta(days=7 * week + store.idx * 2 + rng.randint(0, 1))
            hour, minute = rng.randint(8, 20), rng.randint(0, 59)
            rid = f"{store.name.split()[0].lower()}_{day:%Y%m%d}"
            rec = Receipt(rid, store, day, fmt_date(day, store.date_style, hour, minute))
            for product in rng.sample(CATALOG, rng.randint(5, 8)):
                norm, cat, _, unit, names = product
                unit_price = price_on(store, product, week, seed)
                printed_name = names[store.idx]
                if unit == "lb":
                    w = Decimal(rng.randint(80, 320)) / 100
                    total = r(w * unit_price)
                    rec.lines.append(Line([printed_name, f"  {w} lb @ {money(unit_price)}/lb"], total, "item",
                                          "items", float(w), "lb", unit_price, norm, cat))
                elif rng.random() < 0.25:
                    q = rng.randint(2, 3)
                    rec.lines.append(Line([printed_name, f"  {q} @ {money(unit_price)}"], q * unit_price, "item",
                                          "items", q, "each", unit_price, norm, cat))
                else:
                    rec.lines.append(Line([printed_name], unit_price, "item", "items", None, None, None, norm, cat))
                if rng.random() < 0.15:  # item discount right below the item
                    item_no = len(rec.lines)
                    off = r(Decimal(rec.lines[-1].amount) * Decimal("0.2"))
                    rec.lines.append(Line([f"SALE {printed_name[:14]}"], -off, "discount", "items",
                                          applies_to=item_no))
            receipts.append(rec)

    # Deterministic edge cases on specific receipts (index into the 18 receipts).
    receipts[1].lines.append(Line(["BAG FEE"], 10, "fee", "after_subtotal"))
    receipts[4].lines.append(Line(["STORE COUPON"], -200, "coupon", "after_subtotal"))
    receipts[7].lines.append(Line(["MEMBER SAVINGS"], -150, "coupon", "items"))
    first = receipts[10].lines[0]
    receipts[10].lines.append(Line([f"RETURN {first.printed[0][:16]}"], -first.amount, "refund", "items",
                                   quantity=1, unit="each", unit_price=abs(first.amount),
                                   normalized_name=first.normalized_name, category=first.category))
    receipts[13].print_subtotal = False
    receipts[5].misprint = "subtotal_off_by_100"
    receipts[15].misprint = "total_off_by_10"
    for i, deg in {3: "rotated", 8: "blurred", 11: "noisy", 14: "low_contrast", 16: "heavy_blur"}.items():
        receipts[i].degradation = deg
    # Appended AFTER the original 18 with their own data, so those 18 images stay byte-identical.
    return receipts + egg_pack_receipts(seed)


# Egg cartons of different sizes: (store idx, day, [(printed name, eggs per pack, price cents)], other products)
EGG_PACK_VISITS = [
    (0, date(2026, 11, 12), [("EGGS 1/2 DOZEN", 6, 249), ("EGGS 18CT", 18, 599)], ["milk", "sourdough bread"]),
    (1, date(2026, 11, 14), [("LG EGGS 6CT", 6, 269), ("LG EGGS 24CT", 24, 799)], ["coffee"]),
    (2, date(2026, 11, 16), [("EGGS GRD A 6", 6, 229), ("EGGS GRD A 30", 30, 899)], ["bananas", "spaghetti"]),
    (0, date(2026, 11, 18), [("EGGS DOZEN", 12, 449), ("EGGS 18CT", 18, 579)], ["greek yogurt"]),
]


def egg_pack_receipts(seed: int) -> list[Receipt]:
    rng = random.Random(seed + 100)
    by_name = {p[0]: p for p in CATALOG}
    out = []
    for store_idx, day, eggs, others in EGG_PACK_VISITS:
        store = STORES[store_idx]
        hour, minute = rng.randint(8, 20), rng.randint(0, 59)
        rec = Receipt(f"{store.name.split()[0].lower()}_{day:%Y%m%d}", store, day,
                      fmt_date(day, store.date_style, hour, minute))
        for printed, count, cents in eggs:
            rec.lines.append(Line([printed], cents, "item", "items", None, None, None, "eggs", "dairy_eggs",
                                  pack_count=count))
        for name in others:
            product = by_name[name]
            norm, cat, _, unit, names = product
            price = price_on(store, product, 6, seed)
            if unit == "lb":
                w = Decimal(rng.randint(80, 320)) / 100
                rec.lines.append(Line([names[store_idx], f"  {w} lb @ {money(price)}/lb"], r(w * price), "item",
                                      "items", float(w), "lb", price, norm, cat))
            else:
                rec.lines.append(Line([names[store_idx]], price, "item", "items", None, None, None, norm, cat))
        out.append(rec)
    return out


def totals(rec: Receipt) -> dict:
    items = [l for l in rec.lines if l.section == "items"]
    after = [l for l in rec.lines if l.section == "after_subtotal"]
    subtotal = sum(l.amount for l in items)
    taxable = sum(l.amount for l in items if l.category in TAXABLE)
    tax = r(Decimal(max(taxable, 0)) * rec.store.tax_rate)
    total = subtotal + tax + sum(l.amount for l in after)
    printed_sub, printed_total = subtotal, total
    if rec.misprint == "subtotal_off_by_100":
        printed_sub += 100
    if rec.misprint == "total_off_by_10":
        printed_total += 10
    return dict(subtotal=subtotal, tax=tax, total=total, printed_subtotal=printed_sub, printed_total=printed_total)


def _font(size: int):
    for path in ("C:/Windows/Fonts/consola.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
                 "/System/Library/Fonts/Menlo.ttc"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def render(rec: Receipt, t: dict, rng: random.Random) -> tuple[Image.Image, list[str]]:
    width, row, font = 600, 38, _font(26)
    rows: list[tuple[str, str]] = [(h, "") for h in rec.store.header] + [(rec.date_raw, ""), ("", "")]
    for l in rec.lines:
        if l.section == "after_subtotal":
            continue  # printed below SUBTOTAL, not with the items
        if len(l.printed) == 1:
            rows.append((l.printed[0], money(l.amount)))
        else:
            rows.append((l.printed[0], ""))
            rows.append((l.printed[1], money(l.amount)))
    rows.append(("", ""))
    if rec.print_subtotal:
        rows.append(("SUBTOTAL", money(t["printed_subtotal"])))
    after = [l for l in rec.lines if l.section == "after_subtotal"]
    for l in after:
        rows.append((l.printed[0], money(l.amount)))
    rows.append(("TAX", money(t["tax"])))
    rows.append(("TOTAL", money(t["printed_total"])))
    rows.append(("", ""))
    last4 = f"{rng.randint(0, 9999):04d}"
    auth = f"{rng.randint(0, 999999):06d}"
    member = f"{rng.randint(10**7, 10**8 - 1)}"
    rows += [(f"VISA XXXX XXXX XXXX {last4}", money(t["printed_total"])), (f"AUTH CODE {auth}", ""),
             (f"MEMBER # {member}", "")]
    savings = -sum(l.amount for l in rec.lines if l.kind in ("discount", "coupon"))
    if savings > 0:
        rows.append(("YOU SAVED", money(savings)))  # informational footer; not part of the total
    rows.append(("THANK YOU", ""))

    img = Image.new("RGB", (width, 40 + row * len(rows)), "white")
    d = ImageDraw.Draw(img)
    for i, (left, right) in enumerate(rows):
        y = 20 + row * i
        d.text((24, y), left, fill="black", font=font)
        if right:
            d.text((width - 24 - d.textlength(right, font=font), y), right, fill="black", font=font)
    img = degrade(img, rec.degradation, rng)
    return img, [last4, auth, member]


def degrade(img: Image.Image, kind: str, rng: random.Random) -> Image.Image:
    if kind == "rotated":
        return img.rotate(rng.choice([-3, 3]), expand=True, fillcolor="white")
    if kind == "blurred":
        return img.filter(ImageFilter.GaussianBlur(1.2))
    if kind == "heavy_blur":
        return img.filter(ImageFilter.GaussianBlur(3.5))
    if kind == "noisy":
        px = img.load()
        for _ in range(img.width * img.height // 25):
            x, y = rng.randrange(img.width), rng.randrange(img.height)
            v = rng.randint(0, 255)
            px[x, y] = (v, v, v)
        return img
    if kind == "low_contrast":
        return img.point(lambda v: 150 + v * 90 // 255)
    return img


def label(rec: Receipt, t: dict, sensitive: list[str]) -> dict:
    return {
        "id": rec.id,
        "image": f"receipts/{rec.id}.png",
        "store": rec.store.name,
        "date": rec.day.isoformat(),
        "date_raw": rec.date_raw,
        "currency": "USD",
        "printed_subtotal_cents": t["printed_subtotal"] if rec.print_subtotal else None,
        "tax_cents": t["tax"],
        "printed_total_cents": t["printed_total"],
        "true_subtotal_cents": t["subtotal"],
        "true_total_cents": t["total"],
        # Only a misprint makes the receipt's own numbers inconsistent. A blurry image read correctly should
        # verify; blur is measured by field accuracy, not by the flag.
        "expected_status": "needs_review" if rec.misprint else "verified",
        "misprint": rec.misprint,
        "degradation": rec.degradation,
        "sensitive_values": sensitive,
        "lines": [
            {"printed": " / ".join(l.printed), "kind": l.kind, "section": l.section, "quantity": l.quantity,
             "unit": l.unit, "unit_price_cents": l.unit_price, "line_total_cents": l.amount,
             "applies_to_line": l.applies_to, "normalized_name": l.normalized_name, "category": l.category,
             "pack_count": l.pack_count}
            for l in rec.lines
        ],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    out_img, out_lbl = HERE / "receipts", HERE / "labels"
    out_img.mkdir(exist_ok=True)
    out_lbl.mkdir(exist_ok=True)
    rng = random.Random(args.seed + 1)
    receipts = build_receipts(args.seed)
    for rec in receipts:
        t = totals(rec)
        img, sensitive = render(rec, t, rng)
        img.save(out_img / f"{rec.id}.png")
        (out_lbl / f"{rec.id}.json").write_text(json.dumps(label(rec, t, sensitive), indent=2), encoding="utf-8")
    print(f"wrote {len(receipts)} receipts to {out_img} and labels to {out_lbl}")


if __name__ == "__main__":
    main()
