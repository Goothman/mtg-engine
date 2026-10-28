"""Synthetic "hard" ad photos for before/after evaluation of the recogniser.

    python ml/make_hard_scenes.py --data ml/dataset --out ml/scenes --per-condition 120

Each scene is a whole photo (not a card crop), so it tests detection + recognition together:
  photo        one card, mildly rotated / in perspective
  upside_down  the same, rotated 180 degrees
  sideways     card lying at ~90 degrees
  covered      another card lies on top, hiding 30-50% of it
  pile         4-6 cards scattered and overlapping, the target partly covered
  cut_off      25-40% of the card outside the frame
  glare        hard reflections (lamp/window on a sleeve or slab)
  sleeve       plastic sheen + reflection band

Writes <out>/<condition>/<n>.jpg and <out>/labels.json ([{file, card_id, name, set_code, condition}]).
Targets (lea/leb) and look-alikes (2ed/3ed/ced/cei) are both sampled, to measure recognition
and how often a look-alike would be mistaken for Alpha/Beta. Fixed seed: same scenes every run.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
from pathlib import Path

import cv2
import numpy as np

from augment import glare, random_background, specular

W, H = 1000, 800
CONDITIONS = ["photo", "upside_down", "sideways", "covered", "pile", "cut_off", "glare", "sleeve"]


def load(data: Path, rel: str, h: int) -> np.ndarray:
    img = cv2.imread(str(data / rel))
    return cv2.resize(img, (int(h * 63 / 88), h), interpolation=cv2.INTER_AREA)


def place(canvas: np.ndarray, card: np.ndarray, cx: float, cy: float, angle: float, rng: random.Random) -> np.ndarray:
    """Paste `card` centred at (cx, cy), rotated by `angle` degrees, with slight perspective."""
    h, w = card.shape[:2]
    j = 0.04 * w
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = src + np.float32([[rng.uniform(-j, j), rng.uniform(-j, j)] for _ in range(4)])
    persp = cv2.getPerspectiveTransform(src, dst)
    rot = np.vstack([cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0), [0, 0, 1]])
    shift = np.float32([[1, 0, cx - w / 2], [0, 1, cy - h / 2], [0, 0, 1]])
    m = shift @ rot @ persp
    layer = cv2.warpPerspective(card, m, (canvas.shape[1], canvas.shape[0]))
    mask = cv2.warpPerspective(np.full((h, w), 255, np.uint8), m, (canvas.shape[1], canvas.shape[0])) > 0
    canvas[mask] = layer[mask]
    return canvas


def scene(cond: str, target: dict, pool: list[dict], data: Path, rng: random.Random) -> np.ndarray:
    canvas = random_background(H, W, rng)
    ch = int(H * rng.uniform(0.45, 0.7))
    card = load(data, target["path"], ch)
    cx, cy = W / 2 + rng.uniform(-150, 150), H / 2 + rng.uniform(-60, 60)
    angle = rng.uniform(-15, 15)
    if cond == "upside_down":
        angle += 180
    elif cond == "sideways":
        angle += rng.choice([90, -90])
    elif cond == "cut_off":
        out = rng.uniform(0.25, 0.4)
        if rng.random() < 0.5:
            cx = rng.choice([-1, 1]) * (W / 2 + ch * 63 / 88 * (0.5 - out)) + W / 2
        else:
            cy = rng.choice([-1, 1]) * (H / 2 + ch * (0.5 - out)) + H / 2
    if cond == "pile":
        ch = int(H * rng.uniform(0.35, 0.5))
        card = load(data, target["path"], ch)
        place(canvas, card, cx, cy, angle, rng)
        for _ in range(rng.randint(3, 5)):
            other = load(data, rng.choice(pool)["path"], ch)
            # around the target: some cover its corners/edges, none sits right on its centre
            r, t = rng.uniform(0.55, 1.1) * ch, rng.uniform(0, 2 * np.pi)
            place(canvas, other, cx + r * np.cos(t), cy + r * np.sin(t), rng.uniform(-60, 60), rng)
        return canvas
    if cond == "glare":  # reflections sit on the card (sleeve, toploader, slab), not the table
        card = specular(card, rng)
    elif cond == "sleeve":
        card = specular(glare(card, rng), rng)
    place(canvas, card, cx, cy, angle, rng)
    if cond == "covered":
        other = load(data, rng.choice(pool)["path"], ch)
        cover = rng.uniform(0.3, 0.5)
        t = rng.uniform(0, 2 * np.pi)
        d = (1 - cover) * ch * 0.75
        place(canvas, other, cx + d * np.cos(t), cy + d * np.sin(t), rng.uniform(-40, 40), rng)
    return canvas


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--per-condition", type=int, default=120)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.data / "manifest.csv", encoding="utf-8")))
    rng = random.Random(args.seed)
    targets = [r for r in rows if r["set_code"] in ("lea", "leb")]
    lookalikes = [r for r in rows if r["set_code"] not in ("lea", "leb")]
    labels = []
    for cond in CONDITIONS:
        (args.out / cond).mkdir(parents=True, exist_ok=True)
        n_look = args.per_condition // 4  # a quarter look-alikes
        picks = rng.sample(targets, args.per_condition - n_look) + rng.sample(lookalikes, n_look)
        for i, t in enumerate(picks):
            img = scene(cond, t, rows, args.data, random.Random(f"{args.seed}-{cond}-{i}"))
            name = f"{cond}/{i:03d}.jpg"
            cv2.imwrite(str(args.out / name), img, [cv2.IMWRITE_JPEG_QUALITY, 85])
            labels.append({"file": name, "card_id": t["card_id"], "name": t["name"], "set_code": t["set_code"], "condition": cond})
    (args.out / "labels.json").write_text(json.dumps(labels))
    print(f"wrote {len(labels)} scenes to {args.out}")


if __name__ == "__main__":
    main()
