"""Measure the photo text check (mtg_engine/vision/textcheck.py) on real card scans from Scryfall.

    python ml/eval_textcheck.py --names 60 --out report.json      # needs api.scryfall.com + tesseract

For card names printed in Beta, it reads the Beta, Unlimited, Revised, 4th/5th Edition and foreign
printings, as clean scans and pasted small into a lot-sized photo (card heights 400 / 250 px in an
800 px photo), and counts decisions per printing kind:
  beta / unlimited -> must never be "contradicts" (that would hide a real card)
  later English    -> "contradicts" is right
  foreign          -> "contradicts" is right
"""
from __future__ import annotations

import argparse
import io
import json
import random
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import httpx
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mtg_engine.vision import textcheck  # noqa: E402

API = "https://api.scryfall.com"
KIND = {"leb": "beta", "lea": "beta", "2ed": "unlimited", "3ed": "later", "4ed": "later", "5ed": "later", "6ed": "later"}
FOREIGN_SETS = {"3ed", "4ed", "fbb", "4bb", "5ed", "sum"}
LATIN = {"es", "it", "fr", "de", "pt"}  # languages the check can recognise (Japanese, Chinese... read as unclear)


def get(client: httpx.Client, url: str, **params):
    time.sleep(0.12)  # Scryfall asks for <10 requests/s
    r = client.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r


def printings(client, name: str) -> list[dict]:
    out, url, params = [], f"{API}/cards/search", {"q": f'!"{name}" unique:prints lang:any'}
    while url:
        data = get(client, url, **params).json()
        out += data["data"]
        url, params = data.get("next_page"), {}
    return out


def in_photo(card: np.ndarray, height: int) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    """Paste the card, scaled to `height` px, into an 800x600 'table' photo, as in a lot picture."""
    f = height / card.shape[0]
    c = cv2.resize(card, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
    bg = np.full((600, 800, 3), (60, 70, 80), np.uint8)
    y, x = (600 - c.shape[0]) // 2, (800 - c.shape[1]) // 2
    bg[y:y + c.shape[0], x:x + c.shape[1]] = c
    ok, buf = cv2.imencode(".jpg", bg, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return buf.tobytes(), (x, y, c.shape[1], c.shape[0])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--names", type=int, default=60)
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--ocr", default="tesseract", choices=["tesseract", "paddle"])
    args = ap.parse_args()
    import os

    os.environ["TEXTCHECK_OCR"] = args.ocr
    if args.ocr == "paddle":  # one reader at a time: let it use every core (set before paddle loads)
        os.environ["OMP_THREAD_LIMIT"] = str(os.cpu_count() or 4)
    names = sorted(textcheck.wording())
    random.Random(args.seed).shuffle(names)
    counts: dict[str, Counter] = defaultdict(Counter)
    rows = []
    rng = random.Random(args.seed)

    def check(job):
        name, kind, s, lang, size, content, box = job
        t = time.time()
        v = textcheck.check_photo(content, name, box)
        return {"secs": round(time.time() - t, 2), "name": name, "set": s, "lang": lang, "kind": kind, "size": size, "decision": v.decision,
                "reason": v.reason, "words": v.evidence.get("ocr_words")}

    with httpx.Client(headers={"User-Agent": "mtg-engine-eval", "Accept": "application/json"}) as client, \
            ThreadPoolExecutor(4 if args.ocr == "tesseract" else 1) as pool:  # tesseract runs as a subprocess: threads use every core
        for name in names[: args.names]:
            try:
                prints = printings(client, name)
            except Exception as e:
                print(f"skip {name}: {e}", flush=True)
                continue
            picked = {}
            for p in prints:
                s, lang = p["set"], p.get("lang", "en")
                kind = KIND.get(s) if lang == "en" else ("foreign" if s in FOREIGN_SETS and lang in LATIN else None)
                key = (kind, s if kind == "later" else "", lang if kind == "foreign" else "")
                if kind and key not in picked and p.get("image_uris", {}).get("normal"):
                    picked[key] = (s, lang, p["image_uris"]["normal"])
            foreign = [k for k in picked if k[0] == "foreign"]
            for k in rng.sample(foreign, max(0, len(foreign) - 2)):  # two foreign printings per name
                del picked[k]
            jobs = []
            for (kind, _, _), (s, lang, url) in picked.items():
                try:
                    raw = get(client, url).content
                except Exception as e:
                    print(f"skip image {name} {s}: {e}", flush=True)
                    continue
                card = cv2.cvtColor(np.array(Image.open(io.BytesIO(raw)).convert("RGB")), cv2.COLOR_RGB2BGR)
                ok, buf = cv2.imencode(".jpg", card)
                for size, (content, box) in (("scan", (buf.tobytes(), None)), ("400px", in_photo(card, 400)),
                                             ("250px", in_photo(card, 250))):
                    jobs.append((name, kind, s, lang, size, content, box))
            for r in pool.map(check, jobs):
                rows.append(r)
                with args.out.with_suffix(".rows.jsonl").open("a") as fh:  # partial results survive a timeout
                    fh.write(json.dumps(r) + "\n")
                counts[f"{r['kind']}/{r['size']}"][r["decision"]] += 1
            print(name, [(r["set"], r["lang"], r["size"], r["decision"][:5]) for r in rows[-len(jobs):]], flush=True)
    summary = {k: dict(v) for k, v in sorted(counts.items())}
    for k, v in summary.items():
        print(f"{k:18s} {v}")
    args.out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=1))


if __name__ == "__main__":
    main()
