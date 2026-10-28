"""Fine-tune the card recogniser: identity embedding (SubCenterArcFace) + edition head.

    python ml/train.py --data ml/dataset --out ml/runs/v1 --epochs 30

Identity classes are oracle cards (Alpha and Beta prints of Lightning Bolt share one
identity); the edition head learns lea/leb/2ed/3ed/ced/other from border colour,
corner radius and print details. Augmentations imitate second-hand marketplace photos.

Initialising from the HF base model (dhvazquez/mtg_card_identification_embeddings):
    python ml/train.py ... --backbone <its timm name> --init path/to/checkpoint.(pt|safetensors)
Weights are loaded with strict=False and the number of matched tensors is printed —
check that number; if it is ~0 the architectures differ.

Validation note: Scryfall has one scan per printing, so the per-epoch metrics measure
robustness to *synthetic* photo conditions on seen printings (fixed-seed augmented
views vs clean gallery). They are not a substitute for a labelled set of real ad
photos; build one in Phase 2 and evaluate with ml/evaluate_real.py.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import time
from pathlib import Path

import albumentations as A
import cv2
import numpy as np
import torch
import torch.nn as nn
from pytorch_metric_learning.losses import SubCenterArcFaceLoss
from torch.utils.data import DataLoader, Dataset

from common import EDITION_CLASSES, INPUT_H, INPUT_W, MEAN, STD, CardNet, edition_index

# ---------------------------------------------------------------- augmentation


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
    mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=w / 12)[..., None] * rng.uniform(0.1, 0.4)
    return (img * (1 - mask) + 255 * mask).astype(np.uint8)


def build_aug() -> A.Compose:  # albumentations 1.4.x API
    return A.Compose([
        A.Perspective(scale=(0.02, 0.08), keep_size=True, p=0.7),
        A.Affine(rotate=(-8, 8), shear=(-4, 4), scale=(0.95, 1.05), mode=cv2.BORDER_REPLICATE, p=0.6),
        A.OneOf([A.MotionBlur(blur_limit=7), A.GaussianBlur(blur_limit=(3, 7)), A.Defocus(radius=(1, 3))], p=0.5),
        A.Downscale(scale_min=0.3, scale_max=0.8, p=0.4),
        A.RandomBrightnessContrast(0.3, 0.3, p=0.8),
        A.HueSaturationValue(8, 25, 20, p=0.6),
        A.RandomGamma(p=0.3),
        A.RandomShadow(p=0.2),
        A.GaussNoise(var_limit=(5, 40), p=0.4),
        A.CoarseDropout(max_holes=2, max_height=60, max_width=60, min_height=20, min_width=20, p=0.2),  # fingers
        A.ImageCompression(quality_lower=30, quality_upper=90, p=0.7),
    ])


def to_tensor(img_rgb: np.ndarray) -> torch.Tensor:
    x = cv2.resize(img_rgb, (INPUT_W, INPUT_H), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    x = (x - np.array(MEAN, np.float32)) / np.array(STD, np.float32)
    return torch.from_numpy(x.transpose(2, 0, 1))


# ---------------------------------------------------------------- data


def read_manifest(data: Path) -> list[dict]:
    with open(data / "manifest.csv") as f:
        return list(csv.DictReader(f))


class CardDataset(Dataset):
    def __init__(self, data: Path, rows: list[dict], ident: dict[str, int], repeats: int, augment: bool, seed: int | None = None):
        self.data, self.rows, self.ident, self.repeats, self.augment, self.seed = data, rows, ident, repeats, augment, seed
        self.aug = build_aug()
        self._cache: dict[str, np.ndarray] = {}

    def __len__(self) -> int:
        return len(self.rows) * self.repeats

    def _load(self, rel: str) -> np.ndarray:
        if rel not in self._cache:
            img = cv2.cvtColor(cv2.imread(str(self.data / rel)), cv2.COLOR_BGR2RGB)
            self._cache[rel] = cv2.resize(img, (INPUT_W * 2, INPUT_H * 2), interpolation=cv2.INTER_AREA)
        return self._cache[rel]

    def __getitem__(self, i: int):
        row = self.rows[i % len(self.rows)]
        img = self._load(row["path"])
        if self.augment:
            rng = random.Random(None if self.seed is None else self.seed + i)
            if self.seed is not None:
                np.random.seed(self.seed + i)
                random.seed(self.seed + i)
            img = composite(img, rng)
            if rng.random() < 0.3:
                img = glare(img, rng)
            img = self.aug(image=img)["image"]
        return to_tensor(img), self.ident[row["oracle_id"]], edition_index(row["set_code"])


# ---------------------------------------------------------------- train / eval


@torch.no_grad()
def embed_all(model: nn.Module, loader: DataLoader, device: str):
    model.eval()
    embs, ids, eds, logits = [], [], [], []
    for x, y, e in loader:
        with torch.autocast(device_type=device.split(":")[0], enabled=device.startswith("cuda")):
            emb, lg = model(x.to(device, non_blocking=True))
        embs.append(emb.float().cpu()); logits.append(lg.float().cpu()); ids.append(y); eds.append(e)
    return torch.cat(embs), torch.cat(ids), torch.cat(eds), torch.cat(logits)


def evaluate(model, clean_loader, aug_loader, device) -> dict:
    g_emb, g_id, _, _ = embed_all(model, clean_loader, device)
    q_emb, q_id, q_ed, q_logits = embed_all(model, aug_loader, device)
    nn_idx = (q_emb @ g_emb.T).argmax(1)
    recall1 = (g_id[nn_idx] == q_id).float().mean().item()
    pred_ed = q_logits.argmax(1)
    ed_acc = (pred_ed == q_ed).float().mean().item()
    per = {}
    for i, name in enumerate(EDITION_CLASSES):
        m = q_ed == i
        if m.any():
            per[name] = round((pred_ed[m] == i).float().mean().item(), 4)
    ab = (q_ed <= 1)  # alpha-vs-beta subset
    ab_acc = (pred_ed[ab] == q_ed[ab]).float().mean().item() if ab.any() else float("nan")
    return {"recall@1": round(recall1, 4), "edition_acc": round(ed_acc, 4), "alpha_beta_acc": round(ab_acc, 4), "per_edition": per}


def load_init(model: nn.Module, path: str) -> None:
    if path.endswith(".safetensors"):
        from safetensors.torch import load_file

        sd = load_file(path)
    else:
        sd = torch.load(path, map_location="cpu")
        sd = sd.get("state_dict", sd.get("model", sd)) if isinstance(sd, dict) else sd
    own = model.state_dict()
    matched = {}
    for k, v in sd.items():
        for cand in (k, f"backbone.{k}", k.removeprefix("model."), f"backbone.{k.removeprefix('model.')}", k.removeprefix("backbone.")):
            if cand in own and own[cand].shape == v.shape:
                matched[cand] = v
                break
    model.load_state_dict(matched, strict=False)
    print(f"init: matched {len(matched)}/{len(own)} tensors from {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="ml/dataset")
    ap.add_argument("--out", default="ml/runs/v1")
    ap.add_argument("--backbone", default="tf_efficientnet_lite0")
    ap.add_argument("--init", default=None, help="optional checkpoint to initialise from (e.g. the HF base model)")
    ap.add_argument("--embed-dim", type=int, default=512)
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--repeats", type=int, default=8, help="augmented views per image per epoch")
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--edition-weight", type=float, default=1.0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--no-pretrained", action="store_true", help="random init (offline smoke tests)")
    args = ap.parse_args()

    torch.manual_seed(0)
    data, out = Path(args.data), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    rows = read_manifest(data)
    oracle_ids = sorted({r["oracle_id"] for r in rows})
    ident = {o: i for i, o in enumerate(oracle_ids)}
    print(f"{len(rows)} images, {len(ident)} identities, device={device}")

    train_dl = DataLoader(CardDataset(data, rows, ident, args.repeats, True), batch_size=args.batch, shuffle=True,
                          num_workers=args.workers, pin_memory=True, drop_last=True, persistent_workers=args.workers > 0)
    clean_dl = DataLoader(CardDataset(data, rows, ident, 1, False), batch_size=args.batch, num_workers=args.workers)
    val_dl = DataLoader(CardDataset(data, rows, ident, 2, True, seed=12345), batch_size=args.batch, num_workers=args.workers)

    model = CardNet(args.backbone, args.embed_dim, pretrained=args.init is None and not args.no_pretrained).to(device)
    if args.init:
        load_init(model, args.init)
    arc = SubCenterArcFaceLoss(num_classes=len(ident), embedding_size=args.embed_dim, sub_centers=3, margin=28.6, scale=64).to(device)
    ce = nn.CrossEntropyLoss(label_smoothing=0.05)
    opt = torch.optim.AdamW([
        {"params": model.backbone.parameters(), "lr": args.lr},
        {"params": list(model.embed.parameters()) + list(model.edition.parameters()), "lr": args.lr * 5},
        {"params": arc.parameters(), "lr": args.lr * 20},
    ], weight_decay=1e-4)
    total = args.epochs * len(train_dl)
    warm = max(1, len(train_dl))
    sched = torch.optim.lr_scheduler.LambdaLR(opt, lambda s: min(1.0, (s + 1) / warm) * 0.5 * (1 + math.cos(math.pi * min(s, total) / total)))
    scaler = torch.amp.GradScaler("cuda", enabled=device == "cuda")

    best, history = -1.0, []
    for epoch in range(args.epochs):
        model.train()
        t0, running = time.time(), 0.0
        for x, y, e in train_dl:
            x, y, e = x.to(device, non_blocking=True), y.to(device), e.to(device)
            with torch.autocast(device_type=device, enabled=device == "cuda"):
                emb, logits = model(x)
                loss = arc(emb.float(), y) + args.edition_weight * ce(logits.float(), e)
            opt.zero_grad(set_to_none=True)
            scaler.scale(loss).backward()
            scaler.step(opt)
            scaler.update()
            sched.step()
            running += loss.item()
        metrics = evaluate(model, clean_dl, val_dl, device)
        metrics.update(epoch=epoch, loss=round(running / len(train_dl), 4), secs=round(time.time() - t0))
        history.append(metrics)
        print(json.dumps(metrics))
        score = metrics["recall@1"] + metrics["edition_acc"]
        if score > best:
            best = score
            torch.save({"model": model.state_dict(), "backbone": args.backbone, "embed_dim": args.embed_dim,
                        "identities": oracle_ids, "metrics": metrics}, out / "best.pt")
    (out / "history.json").write_text(json.dumps(history, indent=1))
    print(f"best checkpoint: {out / 'best.pt'}")


if __name__ == "__main__":
    main()
