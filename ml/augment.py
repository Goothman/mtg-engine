"""Photo-condition augmentations shared by training (train.py) and the hard-scene test set
(make_hard_scenes.py). numpy + OpenCV only, so scenes can be generated without torch."""
from __future__ import annotations

import random

import cv2
import numpy as np



def random_background(h: int, w: int, rng: random.Random) -> np.ndarray:
    kind = rng.random()
    if kind < 0.35:  # flat table / cloth
        bg = np.full((h, w, 3), [rng.randint(0, 255) for _ in range(3)], np.uint8)
    elif kind < 0.7:  # gradient
        a, b = np.array([rng.randint(0, 255) for _ in range(3)]), np.array([rng.randint(0, 255) for _ in range(3)])
        t = np.linspace(0, 1, h)[:, None, None]
        bg = (a * (1 - t) + b * t).astype(np.uint8).repeat(w, axis=1)
    else:  # noisy texture (wood/carpet-ish)
        bg = cv2.resize(np.random.randint(0, 255, (h // 16 + 1, w // 16 + 1, 3), np.uint8), (w, h), interpolation=cv2.INTER_CUBIC)
    return np.ascontiguousarray(bg)


def composite(card: np.ndarray, rng: random.Random) -> np.ndarray:
    """Place card on a background. Mostly tight (as after quad detection), sometimes loose."""
    h, w = card.shape[:2]
    scale = rng.uniform(0.9, 0.99) if rng.random() < 0.7 else rng.uniform(0.55, 0.9)
    ch, cw = int(h * scale), int(w * scale)
    canvas = random_background(h, w, rng)
    y, x = rng.randint(0, h - ch), rng.randint(0, w - cw)
    canvas[y : y + ch, x : x + cw] = cv2.resize(card, (cw, ch), interpolation=cv2.INTER_AREA)
    return canvas


def glare(img: np.ndarray, rng: random.Random) -> np.ndarray:
    """Sleeve / toploader reflection: a soft white band."""
    h, w = img.shape[:2]
    mask = np.zeros((h, w), np.float32)
    x0 = rng.randint(-w // 2, w)
    pts = np.array([[x0, 0], [x0 + rng.randint(20, w // 2), 0], [x0 + rng.randint(-w // 3, w // 3), h], [x0 - rng.randint(20, w // 2), h]], np.int32)
    cv2.fillPoly(mask, [pts], 1.0)
    mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=w / 12)[..., None] * rng.uniform(0.1, 0.6)
    return (img * (1 - mask) + 255 * mask).astype(np.uint8)


def specular(img: np.ndarray, rng: random.Random) -> np.ndarray:
    """Hard reflections from a sleeve, toploader or graded slab: bright, almost saturated spots
    (lamp / window), sometimes with the overall washed-out look of plastic."""
    h, w = img.shape[:2]
    out = img.astype(np.float32)
    if rng.random() < 0.4:  # plastic sheen: lower contrast, lifted blacks
        k = rng.uniform(0.75, 0.92)
        out = out * k + 255 * (1 - k) * rng.uniform(0.4, 1.0)
    for _ in range(rng.randint(1, 3)):
        mask = np.zeros((h, w), np.float32)
        center = (rng.randint(0, w), rng.randint(0, h))
        axes = (rng.randint(w // 20, w // 4), rng.randint(h // 30, h // 6))
        cv2.ellipse(mask, center, axes, rng.uniform(0, 180), 0, 360, 1.0, -1)
        mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=max(axes) / 3)[..., None] * rng.uniform(0.5, 0.97)
        out = out * (1 - mask) + 255 * mask
    return np.clip(out, 0, 255).astype(np.uint8)


def overlap(img: np.ndarray, other: np.ndarray, rng: random.Random) -> np.ndarray:
    """Another card lying on top (scrambled piles): it covers 15-55% of this one."""
    h, w = img.shape[:2]
    s = rng.uniform(0.8, 1.1)
    oh, ow = int(h * s), int(w * s)
    other = cv2.resize(other, (ow, oh), interpolation=cv2.INTER_AREA)
    cover = rng.uniform(0.15, 0.55)
    side = rng.choice("tblr")
    # top-left corner of the covering card so that roughly `cover` of the target is hidden
    x = rng.randint(-ow // 3, w - ow // 2) if side in "tb" else (int(w * (1 - cover)) if side == "r" else int(w * cover) - ow)
    y = rng.randint(-oh // 3, h - oh // 2) if side in "lr" else (int(h * (1 - cover)) if side == "b" else int(h * cover) - oh)
    m = cv2.getRotationMatrix2D((ow / 2, oh / 2), rng.uniform(-35, 35), 1.0)
    m[:, 2] += (x, y)
    layer = cv2.warpAffine(other, m, (w, h))
    mask = cv2.warpAffine(np.full((oh, ow), 255, np.uint8), m, (w, h)) > 0
    out = img.copy()
    out[mask] = layer[mask]
    return out


def cut_off(img: np.ndarray, rng: random.Random) -> np.ndarray:
    """Card partly outside the photo: one side replaced by background."""
    h, w = img.shape[:2]
    keep = rng.uniform(0.55, 0.9)
    bg = random_background(h, w, rng)
    side = rng.choice("tblr")
    out = img.copy()
    if side == "t":
        out[: int(h * (1 - keep))] = bg[: int(h * (1 - keep))]
    elif side == "b":
        out[int(h * keep):] = bg[int(h * keep):]
    elif side == "l":
        out[:, : int(w * (1 - keep))] = bg[:, : int(w * (1 - keep))]
    else:
        out[:, int(w * keep):] = bg[:, int(w * keep):]
    return out
