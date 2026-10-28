"""Evaluate the exported model on real, hand-labelled ad photos.

    python ml/evaluate_real.py --labels ml/real_eval/labels.csv --models models/

labels.csv columns: path, card_id (Scryfall id of the true printing). Uses the same
code path as production (mtg_engine.vision.recognizer), so it measures what the scanner sees.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mtg_engine.vision.recognizer import CardRecognizer  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", required=True)
    ap.add_argument("--models", default="models")
    args = ap.parse_args()
    rec = CardRecognizer(Path(args.models) / "card_model.onnx", Path(args.models) / "gallery.npz")
    import numpy as np

    g = np.load(Path(args.models) / "gallery.npz")
    name_of = dict(zip(g["card_ids"], g["names"]))
    set_of = dict(zip(g["card_ids"], g["set_codes"]))
    base = Path(args.labels).parent
    c = Counter()
    for row in csv.DictReader(open(args.labels)):
        recs = rec.recognize_image((base / row["path"]).read_bytes())
        if not recs:
            c["no_detection"] += 1
            continue
        best = max(recs, key=lambda r: r.similarity)
        c["total"] += 1
        c["name_ok"] += name_of.get(best.card_id) == name_of.get(row["card_id"])
        c["printing_ok"] += best.card_id == row["card_id"]
        ed = max(best.edition_probs, key=best.edition_probs.get)
        c["edition_ok"] += ed == {"cei": "ced"}.get(set_of.get(row["card_id"]), set_of.get(row["card_id"]))
    t = max(c["total"], 1)
    print({"images": c["total"], "no_detection": c["no_detection"], "name_acc": c["name_ok"] / t,
           "printing_acc": c["printing_ok"] / t, "edition_acc": c["edition_ok"] / t})


if __name__ == "__main__":
    main()
