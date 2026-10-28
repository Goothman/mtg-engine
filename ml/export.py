"""Export the trained model to ONNX and build the retrieval gallery used by mtg_engine.vision.

    python ml/export.py --ckpt ml/runs/v1/best.pt --data ml/dataset --out models/

Produces models/card_model.onnx and models/gallery.npz (embeddings of every clean scan in
the manifest, keyed by Scryfall id, plus preprocessing metadata).
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import torch
from torch.utils.data import DataLoader

from common import EDITION_CLASSES, INPUT_H, INPUT_W, MEAN, STD, CardNet
from train import CardDataset, embed_all, read_manifest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", default="ml/runs/v1/best.pt")
    ap.add_argument("--data", default="ml/dataset")
    ap.add_argument("--out", default="models")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    ck = torch.load(args.ckpt, map_location="cpu")
    model = CardNet(ck["backbone"], ck["embed_dim"], pretrained=False)
    model.load_state_dict(ck["model"])
    model.eval()

    dummy = torch.randn(1, 3, INPUT_H, INPUT_W)
    kwargs = dict(input_names=["image"], output_names=["embedding", "edition_logits"],
                  dynamic_axes={"image": {0: "batch"}, "embedding": {0: "batch"}, "edition_logits": {0: "batch"}}, opset_version=17)
    try:  # classic TorchScript exporter: fewest dependencies
        torch.onnx.export(model, dummy, out / "card_model.onnx", dynamo=False, **kwargs)
    except TypeError:  # older torch without the `dynamo` flag
        torch.onnx.export(model, dummy, out / "card_model.onnx", **kwargs)

    rows = read_manifest(Path(args.data))
    ident = {o: i for i, o in enumerate(sorted({r["oracle_id"] for r in rows}))}
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device)
    emb, _, _, _ = embed_all(model, DataLoader(CardDataset(Path(args.data), rows, ident, 1, False), batch_size=64), device)
    np.savez_compressed(
        out / "gallery.npz",
        embeddings=emb.numpy().astype(np.float32),
        card_ids=np.array([r["card_id"] for r in rows]),
        set_codes=np.array([r["set_code"] for r in rows]),
        names=np.array([r["name"] for r in rows]),
        input_h=INPUT_H, input_w=INPUT_W, mean=np.array(MEAN), std=np.array(STD),
        edition_classes=np.array(EDITION_CLASSES),
    )
    print(f"wrote {out / 'card_model.onnx'} and {out / 'gallery.npz'} ({len(rows)} gallery images); metrics at save: {ck.get('metrics')}")


if __name__ == "__main__":
    main()
