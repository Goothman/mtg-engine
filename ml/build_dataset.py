"""Download card scans from Scryfall for training/gallery.

    python ml/build_dataset.py --sets lea leb 2ed 3ed ced cei --out ml/dataset

Writes <out>/images/<set>/<scryfall_id>.jpg and <out>/manifest.csv
(columns: path, card_id, name, oracle_id, set_code). Scryfall asks for
50-100 ms between requests; image CDN downloads are not rate limited but be polite.
"""
from __future__ import annotations

import argparse
import csv
import time
from pathlib import Path

import httpx

API = "https://api.scryfall.com/cards/search"
HEADERS = {"User-Agent": "mtg-engine-dataset/0.1", "Accept": "application/json"}


def fetch_set(client: httpx.Client, code: str) -> list[dict]:
    out, url, params = [], API, {"q": f"set:{code}", "unique": "prints"}
    while url:
        r = client.get(url, params=params)
        r.raise_for_status()
        data = r.json()
        out.extend(data["data"])
        url, params = (data.get("next_page") if data.get("has_more") else None), None
        time.sleep(0.12)
    return out


def image_url(card: dict, size: str) -> str | None:
    uris = card.get("image_uris") or (card.get("card_faces") or [{}])[0].get("image_uris") or {}
    return uris.get(size) or uris.get("large") or uris.get("normal")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sets", nargs="+", default=["lea", "leb", "2ed", "3ed", "ced", "cei"])
    ap.add_argument("--out", default="ml/dataset")
    ap.add_argument("--size", default="large", choices=["normal", "large", "png"])
    args = ap.parse_args()

    out = Path(args.out)
    rows = []
    with httpx.Client(headers=HEADERS, timeout=60, follow_redirects=True) as client:
        for code in args.sets:
            cards = fetch_set(client, code)
            print(f"{code}: {len(cards)} printings")
            (out / "images" / code).mkdir(parents=True, exist_ok=True)
            for c in cards:
                url = image_url(c, args.size)
                if not url:
                    continue
                ext = "png" if args.size == "png" else "jpg"
                path = out / "images" / code / f"{c['id']}.{ext}"
                if not path.exists():
                    r = client.get(url)
                    r.raise_for_status()
                    path.write_bytes(r.content)
                    time.sleep(0.05)
                rows.append({"path": str(path.relative_to(out)), "card_id": c["id"], "name": c["name"],
                             "oracle_id": c.get("oracle_id") or c["name"], "set_code": c["set"]})
    with open(out / "manifest.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["path", "card_id", "name", "oracle_id", "set_code"])
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} rows to {out / 'manifest.csv'}")


if __name__ == "__main__":
    main()
