"""Score the full recogniser (detection + model) on the hard scenes from make_hard_scenes.py.

    python ml/eval_scenes.py --scenes path/to/scenes --model models/card_model.onnx --gallery models/gallery.npz
    python ml/eval_scenes.py ... --recognizer path/to/old/recognizer.py   # compare another detector version

Per condition, for Alpha/Beta scenes:
  found      the card's name is among the readings (similarity >= 0.55)
  edition    ... and that reading's edition is right
  photo_ok   ... and it clears the photo-only bar (similarity >= 0.7, its Alpha/Beta edition >= 0.7)
and for look-alike scenes (Unlimited, Revised, CE):
  false_ab   a reading of the right card clears the photo-only bar as Alpha/Beta (lower is better)
Card names come from the local catalog (data/mtg_engine.db), since gallery ids are printings.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {"lea", "leb"}


def load_recognizer(path: Path | None):
    if path is None:
        sys.path.insert(0, str(ROOT))
        from mtg_engine.vision.recognizer import CardRecognizer
        return CardRecognizer
    spec = importlib.util.spec_from_file_location("recognizer_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses look their module up here
    spec.loader.exec_module(mod)
    return mod.CardRecognizer


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenes", type=Path, required=True)
    ap.add_argument("--model", type=Path, required=True)
    ap.add_argument("--gallery", type=Path, required=True)
    ap.add_argument("--recognizer", type=Path, help="alternative recognizer.py (default: the installed one)")
    ap.add_argument("--db", type=Path, default=ROOT / "data" / "mtg_engine.db")
    ap.add_argument("--limit", type=int, help="scenes per condition")
    ap.add_argument("--json", type=Path, help="write the summary here")
    args = ap.parse_args()

    names = dict(sqlite3.connect(args.db).execute("select id, name from card_printings"))
    rec = load_recognizer(args.recognizer)(args.model, args.gallery)
    labels = json.loads((args.scenes / "labels.json").read_text())
    per_cond: dict[str, list] = defaultdict(list)
    for lab in labels:
        if args.limit and len(per_cond[lab["condition"]]) >= args.limit:
            continue
        per_cond[lab["condition"]].append(lab)

    summary = {}
    for cond, labs in per_cond.items():
        c = defaultdict(int)
        for lab in labs:
            reads = rec.recognize_image((args.scenes / lab["file"]).read_bytes())
            mine = [r for r in reads if names.get(r.card_id) == lab["name"] and r.similarity >= 0.55]
            target = lab["set_code"] in TARGETS
            c["targets" if target else "lookalikes"] += 1
            for r in mine[:1] if mine else []:
                ed, p = max(r.edition_probs.items(), key=lambda kv: kv[1])
                photo_ok = r.similarity >= 0.7 and ed in TARGETS and p >= 0.7
                if target:
                    c["found"] += 1
                    c["edition"] += ed == lab["set_code"]
                    c["photo_ok"] += photo_ok
                else:
                    c["false_ab"] += photo_ok
        t, lk = max(c["targets"], 1), max(c["lookalikes"], 1)
        summary[cond] = {"n": c["targets"] + c["lookalikes"], "found": round(c["found"] / t, 3),
                         "edition": round(c["edition"] / t, 3), "photo_ok": round(c["photo_ok"] / t, 3),
                         "false_ab": round(c["false_ab"] / lk, 3)}
        print(f"{cond:12s} {json.dumps(summary[cond])}", flush=True)
    if args.json:
        args.json.write_text(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
