"""Image-based card recognition (optional; pip install '.[vision]').

Pipeline per ad photo:
  1. detect card-shaped quadrilaterals (OpenCV contours) and warp each one upright;
     the whole photo is scored too unless detected cards cover most of it (close-ups).
     When fewer than two clean cards are found (scrambled piles, cards partly covered by
     others or cut off by the frame), card-sized sliding windows in both orientations are
     scored as well.
  2. embed each crop with the ONNX model trained in `ml/` (identity embedding + edition logits),
     also upside down (the detector can't tell top from bottom), and with glare removed when
     the crop has blown-out reflections (sleeves, toploaders, slabs); the best view wins.
  3. cosine kNN against `gallery.npz` (embeddings of clean Scryfall scans).

The gallery file carries its own preprocessing metadata, so model and gallery
always agree: input_h, input_w, mean, std, edition_classes.
"""
from __future__ import annotations

import hashlib
import io
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

CARD_ASPECT = 63 / 88  # width / height
DETECTOR_VERSION = "2"  # part of the cache fingerprint: bump when detection/TTA changes
WINDOW_MIN_SIMILARITY = 0.45  # window crops are many; keep only the plausible ones


@dataclass
class Recognition:
    card_id: str
    similarity: float
    edition_probs: dict[str, float]
    box: tuple[int, int, int, int] | None


def _order_quad(pts: np.ndarray) -> np.ndarray:
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]], dtype=np.float32)


def detect_cards(img_bgr: np.ndarray, max_cards: int = 12) -> list[tuple[np.ndarray, tuple[int, int, int, int]]]:
    import cv2

    h, w = img_bgr.shape[:2]
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 40, 120)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    min_area = 0.01 * h * w
    for c in sorted(contours, key=cv2.contourArea, reverse=True)[: max_cards * 3]:
        if cv2.contourArea(c) < min_area:
            break
        approx = cv2.approxPolyDP(c, 0.03 * cv2.arcLength(c, True), True)
        if len(approx) != 4:
            continue
        quad = _order_quad(approx.reshape(4, 2).astype(np.float32))
        wa = np.linalg.norm(quad[1] - quad[0])
        ha = np.linalg.norm(quad[3] - quad[0])
        if wa > ha:  # landscape card: rotate corner order
            quad = np.roll(quad, -1, axis=0)
            wa, ha = ha, wa
        if not 0.55 < wa / max(ha, 1) < 0.9:
            continue
        dst = np.array([[0, 0], [251, 0], [251, 351], [0, 351]], dtype=np.float32)
        warped = cv2.warpPerspective(img_bgr, cv2.getPerspectiveTransform(quad, dst), (252, 352))
        x, y, bw, bh = cv2.boundingRect(approx)
        out.append((warped, (x, y, bw, bh)))
        if len(out) >= max_cards:
            break
    return out


def card_windows(img_bgr: np.ndarray, max_windows: int = 24) -> list[tuple[np.ndarray, tuple[int, int, int, int]]]:
    """Card-shaped sliding windows (portrait and landscape) for piles where contours fail."""
    import cv2

    h, w = img_bgr.shape[:2]
    out = []
    for frac in (0.8, 0.5):
        for landscape in (False, True):
            wh = int(min(h, w) * frac)
            ww = int(wh * CARD_ASPECT)
            if landscape:
                ww, wh = wh, ww
            if ww > w or wh > h:
                continue
            for y in range(0, h - wh + 1, max(1, wh // 2)):
                for x in range(0, w - ww + 1, max(1, ww // 2)):
                    crop = img_bgr[y : y + wh, x : x + ww]
                    if landscape:
                        crop = cv2.rotate(crop, cv2.ROTATE_90_CLOCKWISE)
                    out.append((crop, (x, y, ww, wh)))
    if len(out) > max_windows:  # spread the budget evenly
        idx = np.linspace(0, len(out) - 1, max_windows).round().astype(int)
        out = [out[i] for i in idx]
    return out


def remove_glare(img_bgr: np.ndarray) -> np.ndarray | None:
    """Inpaint blown-out reflections; None if the crop has no significant glare."""
    import cv2

    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    mask = ((hsv[..., 2] > 240) & (hsv[..., 1] < 40)).astype(np.uint8) * 255
    if mask.mean() / 255 < 0.02:
        return None
    mask = cv2.dilate(mask, np.ones((5, 5), np.uint8))
    return cv2.inpaint(img_bgr, mask, 5, cv2.INPAINT_TELEA)


class CardRecognizer:
    def __init__(self, model_path: str | Path, gallery_path: str | Path, closeup_margin: float = 0.05):
        import onnxruntime as ort

        self.closeup_margin = closeup_margin
        h = hashlib.sha256(DETECTOR_VERSION.encode())
        for p in (model_path, gallery_path):
            h.update(Path(p).read_bytes())
        self.fingerprint = h.hexdigest()[:16]  # cache key: results are reused only for the same model
        self.session = ort.InferenceSession(str(model_path), providers=ort.get_available_providers())
        g = np.load(gallery_path, allow_pickle=False)
        self.embeddings = g["embeddings"].astype(np.float32)
        self.card_ids = [str(x) for x in g["card_ids"]]
        self.input_hw = (int(g["input_h"]), int(g["input_w"]))
        self.mean = g["mean"].astype(np.float32).reshape(1, 3, 1, 1)
        self.std = g["std"].astype(np.float32).reshape(1, 3, 1, 1)
        self.edition_classes = [str(x) for x in g["edition_classes"]]
        self.input_name = self.session.get_inputs()[0].name

    def _prep(self, crops_bgr: list[np.ndarray]) -> np.ndarray:
        import cv2

        h, w = self.input_hw
        batch = [cv2.resize(cv2.cvtColor(c, cv2.COLOR_BGR2RGB), (w, h)) for c in crops_bgr]
        x = np.stack(batch).astype(np.float32).transpose(0, 3, 1, 2) / 255.0
        return (x - self.mean) / self.std

    def embed(self, crops_bgr: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
        emb, logits = self.session.run(None, {self.input_name: self._prep(crops_bgr)})
        emb = emb / np.linalg.norm(emb, axis=1, keepdims=True).clip(1e-6)
        e = np.exp(logits - logits.max(axis=1, keepdims=True))
        return emb, e / e.sum(axis=1, keepdims=True)

    def _best_views(self, crops: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
        """Similarity rows and edition probs per crop, keeping the best of its views
        (as is, upside down, glare removed)."""
        import cv2

        views, owner = [], []
        for i, c in enumerate(crops):
            for v in (c, cv2.rotate(c, cv2.ROTATE_180)):
                views.append(v)
                owner.append(i)
            g = remove_glare(c)
            if g is not None:
                views.append(g)
                owner.append(i)
        sims_all, probs_all = [], []
        for k in range(0, len(views), 32):  # bounded batches
            emb, probs = self.embed(views[k : k + 32])
            sims_all.append(emb @ self.embeddings.T)
            probs_all.append(probs)
        sims_all, probs_all = np.concatenate(sims_all), np.concatenate(probs_all)
        owner = np.array(owner)
        best_view = [np.flatnonzero(owner == i)[sims_all[owner == i].max(axis=1).argmax()] for i in range(len(crops))]
        return sims_all[best_view], probs_all[best_view]

    def recognize_image(self, image_bytes: bytes, top_k: int = 1) -> list[Recognition]:
        import cv2
        from PIL import Image

        img = cv2.cvtColor(np.array(Image.open(io.BytesIO(image_bytes)).convert("RGB")), cv2.COLOR_RGB2BGR)
        found = detect_cards(img)
        quads = len(found)
        h, w = img.shape[:2]
        # Close-ups: the card fills the frame and the detector may lock onto the art box,
        # so also score the whole photo unless detected cards cover most of it.
        whole = sum(b[2] * b[3] for _, b in found) < 0.5 * h * w
        if whole:
            found.append((img, None))
        windows = card_windows(img) if quads < 2 else []
        crops = [c for c, _ in found] + [c for c, _ in windows]
        sims, probs = self._best_views(crops)
        best = sims.max(axis=1)
        keep = list(range(quads))
        if whole:
            # Whole photo vs detected crops: if the whole photo matches clearly better than every
            # crop, it's a single-card close-up (crops were card parts); otherwise trust the crops.
            wi = quads
            if not quads or best[wi] >= best[:quads].max() + self.closeup_margin:
                keep = [wi]
        boxes = [b for _, b in found] + [b for _, b in windows]
        out = []
        for i in keep:
            out += self._top(sims[i], probs[i], boxes[i], top_k)
        # windows: one reading per card name (its best window), only plausible ones
        seen = {r.card_id for r in out}
        base = len(found)
        for i in sorted(range(base, len(crops)), key=lambda i: -best[i]):
            if best[i] < WINDOW_MIN_SIMILARITY:
                break
            for r in self._top(sims[i], probs[i], boxes[i], 1):
                if r.card_id not in seen:
                    seen.add(r.card_id)
                    out.append(r)
        if not out:  # keep the single best reading so callers know how (dis)similar the photo is
            i = int(best.argmax())
            out = self._top(sims[i], probs[i], boxes[i], 1)
        return out

    def _top(self, sim_row: np.ndarray, prob_row: np.ndarray, box, top_k: int) -> list[Recognition]:
        return [
            Recognition(
                card_id=self.card_ids[j],
                similarity=float(sim_row[j]),
                edition_probs={c: float(p) for c, p in zip(self.edition_classes, prob_row)},
                box=box,
            )
            for j in np.argsort(-sim_row)[:top_k]
        ]
