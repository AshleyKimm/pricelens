"""Download a fixed, seeded sample of CORD-v2 receipts for the eval (stdlib only, no new dependency).

    python evals/download_cord.py [--per-split 15] [--seed 2026]

CORD-v2: https://huggingface.co/datasets/naver-clova-ix/cord-v2 (Clova AI / NAVER), CC BY 4.0.
Files go to evals/data/cord/ (gitignored: dataset images are never committed).
Uses the public Hugging Face datasets-server JSON API; image URLs are signed and short-lived,
so images are downloaded immediately. Re-running skips files that already exist.
"""
from __future__ import annotations

import argparse
import json
import random
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://datasets-server.huggingface.co/rows"
DATASET = "naver-clova-ix/cord-v2"
OUT = Path(__file__).parent / "data" / "cord"
HEADERS = {"User-Agent": "pricelens-eval/0.1"}  # generic client name, no personal info
ATTRIBUTION = ("CORD-v2 (Clova AI / NAVER), https://huggingface.co/datasets/naver-clova-ix/cord-v2, "
               "licensed CC BY 4.0. Images are not redistributed by this repository.")


def get_json(url: str) -> dict:
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=60) as r:
                return json.load(r)
        except OSError:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)


def fetch_rows(split: str) -> list[dict]:
    rows, offset, total = [], 0, None
    while total is None or offset < total:
        q = urllib.parse.urlencode({"dataset": DATASET, "config": "default", "split": split,
                                    "offset": offset, "length": 100})
        data = get_json(f"{API}?{q}")
        total = data["num_rows_total"]
        rows += data["rows"]
        offset += len(data["rows"])
        if not data["rows"]:
            break
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-split", type=int, default=15)
    ap.add_argument("--seed", type=int, default=2026)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = []
    for split in ("validation", "test"):
        rows = fetch_rows(split)
        picks = sorted(random.Random(f"{args.seed}-{split}").sample(range(len(rows)), args.per_split))
        for idx in picks:
            row = rows[idx]
            stem = f"{split}_{row['row_idx']:03d}"
            img, gt = OUT / f"{stem}.jpg", OUT / f"{stem}.json"
            if not img.exists():
                with urllib.request.urlopen(urllib.request.Request(row["row"]["image"]["src"], headers=HEADERS),
                                            timeout=60) as r:
                    img.write_bytes(r.read())
            if not gt.exists():
                full = json.loads(row["row"]["ground_truth"])
                gt.write_text(json.dumps({"gt_parse": full["gt_parse"], "meta": full.get("meta")}, indent=1,
                                         ensure_ascii=False), encoding="utf-8")
            manifest.append(stem)
        print(f"{split}: {len(rows)} rows, sampled {len(picks)}")
    (OUT / "SOURCE.txt").write_text(ATTRIBUTION + "\n\nSample: " + ", ".join(manifest) + "\n", encoding="utf-8")
    print(f"wrote {len(manifest)} receipts to {OUT}")


if __name__ == "__main__":
    main()
