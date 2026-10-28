"""Download the HF base model checkpoint (dhvazquez/mtg_card_identification_embeddings, MIT).

    python ml/fetch_base_model.py --out ml/base   # prints the local best.pt path
"""
from __future__ import annotations

import argparse

from huggingface_hub import hf_hub_download

REPO = "dhvazquez/mtg_card_identification_embeddings"
RUN = "20260521-044732"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="ml/base")
    args = ap.parse_args()
    path = hf_hub_download(REPO, f"{RUN}/best.pt", local_dir=args.out)
    for extra in ("config.yaml", "metrics.json"):
        hf_hub_download(REPO, f"{RUN}/{extra}", local_dir=args.out)
    print(path)


if __name__ == "__main__":
    main()
