"""The pre-computed public demo: rebuilt from committed files only, with zero API calls.

  demo/extractions/<sha>.json  - real model output for each synthetic receipt (saved during live runs)
  demo/images/<sha>.jpg        - the redacted image that was sent (fake data, safe to commit)
  demo/labels.json             - Layer 2 labels from the live run, including human corrections

`export` writes those files from a working database; `build` replays them through the real
verify-and-store code into data/demo.db.
"""
from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path

from . import db
from .extract import ReplayExtractor
from .pipeline import add_receipt
from .redact import PreparedImage, sha256_file

# Verification is relative to "today" (future / too-old date checks). Pin it so the demo is the
# same no matter when it's built.
DEMO_AS_OF = date(2026, 11, 21)
LABEL_FIELDS = ("description_key", "description", "normalized_name", "variety", "category", "confidence",
                "needs_review", "review_reasons", "source", "model", "prompt_version",
                "pack_quantity", "pack_unit", "pack_base_qty", "pack_base_unit")


def export(conn, root: Path) -> dict:
    demo = root / "demo"
    (demo / "images").mkdir(parents=True, exist_ok=True)
    copied = 0
    for (sha,) in conn.execute("SELECT image_sha256 FROM receipts WHERE source = 'public'"):
        src = root / "data" / "redacted" / f"{sha}.jpg"
        if src.exists():
            shutil.copyfile(src, demo / "images" / f"{sha}.jpg")
            copied += 1
    rows = conn.execute(f"""SELECT s.name AS store, {', '.join('l.' + f for f in LABEL_FIELDS)}
                            FROM item_labels l JOIN stores s ON s.id = l.store_id
                            WHERE l.store_id IN (SELECT store_id FROM receipts WHERE source = 'public')
                            ORDER BY s.name, l.description_key""").fetchall()
    (demo / "labels.json").write_text(json.dumps([dict(r) for r in rows], indent=1), encoding="utf-8")
    return {"images": copied, "labels": len(rows)}


def build(root: Path, out: Path) -> dict:
    demo = root / "demo"
    fixtures = demo / "extractions"
    if out.exists():
        out.unlink()
    conn = db.connect(out)
    added = 0
    # add_receipt() saves a copy of the image it "sent"; keep that out of the repo (the web app serves
    # demo/images directly, by hash).
    scratch = out.parent / ".demo_build"
    for path in sorted((root / "evals" / "synthetic" / "receipts").glob("*.png")):
        sha = sha256_file(path)
        image = demo / "images" / f"{sha}.jpg"
        if not (fixtures / f"{sha}.json").exists() or not image.exists():
            continue
        add_receipt(path, conn=conn, extractor=ReplayExtractor(fixtures), redacted_dir=scratch,
                    prepare_fn=lambda p, sha=sha, image=image: PreparedImage(sha, image.read_bytes(), [], 0),
                    today=DEMO_AS_OF, allow_uncertain_redaction=True)  # already-redacted committed images
        added += 1
    shutil.rmtree(scratch, ignore_errors=True)
    labels = json.loads((demo / "labels.json").read_text(encoding="utf-8")) if (demo / "labels.json").exists() else []
    with conn:
        for l in labels:
            store = conn.execute("SELECT id FROM stores WHERE name = ?", (l["store"],)).fetchone()
            if store is None:
                continue
            conn.execute(f"""INSERT INTO item_labels(store_id, {', '.join(LABEL_FIELDS)})
                             VALUES (?, {', '.join('?' * len(LABEL_FIELDS))})""",
                         (store[0], *(l.get(f) for f in LABEL_FIELDS)))  # .get: older exports lack newer fields
    n_labels = conn.execute("SELECT COUNT(*) FROM item_labels").fetchone()[0]
    conn.close()
    return {"receipts": added, "labels": n_labels}
