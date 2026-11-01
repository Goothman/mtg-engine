"""Build mtg_engine/data/edition_texts.json.gz: how each Alpha/Beta card is worded in every old edition.

    python scripts/edition_texts.py        # runs in GitHub Actions (mtgjson.com isn't reachable from every sandbox)

Wording is taken from MTGJSON's `originalText`/`originalType` (the text as printed on that card), plus
the foreign-language printings (`foreignData`), for every card name printed in Alpha or Beta.
The photo text check (mtg_engine/vision/textcheck.py) compares what it reads on a card with these.
Shape: {"sets": {code: name}, "cards": {name: {"en": {set: {"type", "text", "flavor"}}, "foreign": {lang: [text...]}}}}
"""
from __future__ import annotations

import gzip
import json
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "mtg_engine" / "data" / "edition_texts.json.gz"
TARGETS = ["LEA", "LEB"]
# English printings an Alpha/Beta card name can turn up as in a lot (core sets, reprint sets)
OTHERS = ["2ED", "3ED", "4ED", "5ED", "6ED", "7ED", "8ED", "9ED", "10E", "CED", "CEI", "SUM", "CHR", "ME4", "M10", "M11"]


def fetch(code: str) -> dict:
    req = urllib.request.Request(f"https://mtgjson.com/api/v5/{code}.json", headers={"User-Agent": "mtg-engine"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["data"]


def main() -> None:
    sets, cards = {}, {}
    for code in TARGETS + OTHERS:
        try:
            data = fetch(code)
        except Exception as e:  # a set missing upstream shouldn't stop the rest
            print(f"skip {code}: {e}")
            continue
        sets[code.lower()] = data["name"]
        for c in data["cards"]:
            name = c["name"]
            if code not in TARGETS and name not in cards:
                continue
            entry = cards.setdefault(name, {"en": {}, "foreign": {}})
            # printed wording only: today's Oracle wording would look like a later edition
            if c.get("originalText") or c.get("originalType"):
                entry["en"].setdefault(code.lower(), {"type": c.get("originalType") or "", "text": c.get("originalText") or "",
                                                      "flavor": c.get("flavorText") or ""})
            for f in c.get("foreignData") or []:
                text = " ".join(x for x in (f.get("type"), f.get("text"), f.get("flavorText")) if x)
                if text and text not in entry["foreign"].setdefault(f["language"], []):
                    entry["foreign"][f["language"]].append(text)
        print(f"{code}: {len(data['cards'])} cards")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8") as fh:
        json.dump({"sets": sets, "cards": cards}, fh, ensure_ascii=False, sort_keys=True)
    print(f"wrote {OUT} ({len(cards)} card names)")


if __name__ == "__main__":
    main()
