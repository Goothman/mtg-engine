"""Image-based card recognition (optional; pip install '.[vision]').

Pipeline per ad photo:
  1. detect card-shaped quadrilaterals (OpenCV contours) and warp each one upright;
     if none is found, the whole photo is used (single-card close-ups).
  2. embed each crop with the ONNX model trained in `ml/` (identity embedding + edition logits).
  3. cosine kNN against `gallery.npz` (embeddings of clean Scryfall scans).

The gallery file carries its own preprocessing metadata, so model and gallery
always agree: input_h, input_w, mean, std, edition_classes.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

CARD_ASPECT = 63 / 88  # width / height


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


class CardRecognizer:
    def __init__(self, model_path: str | Path, gallery_path: str | Path):
        import onnxruntime as ort

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

    def recognize_image(self, image_bytes: bytes, top_k: int = 1) -> list[Recognition]:
        import cv2
        from PIL import Image

        img = cv2.cvtColor(np.array(Image.open(io.BytesIO(image_bytes)).convert("RGB")), cv2.COLOR_RGB2BGR)
        found = detect_cards(img) or [(img, None)]
        emb, probs = self.embed([c for c, _ in found])
        sims = emb @ self.embeddings.T
        out = []
        for i, (_, box) in enumerate(found):
            for j in np.argsort(-sims[i])[:top_k]:
                out.append(
                    Recognition(
                        card_id=self.card_ids[j],
                        similarity=float(sims[i, j]),
                        edition_probs={c: float(p) for c, p in zip(self.edition_classes, probs[i])},
                        box=box,
                    )
                )
        return out
