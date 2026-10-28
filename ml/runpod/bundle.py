"""Pack ml/ into a base64 tar.gz small enough to pass to a pod as an env var.

Used instead of `git clone` so a private repo needs no credentials on the pod.
The pod unpacks it with:  echo "$ML_BUNDLE" | base64 -d | tar xz -C /workspace/src
"""
from __future__ import annotations

import base64
import io
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILES = ["ml/build_dataset.py", "ml/common.py", "ml/train.py", "ml/export.py", "ml/fetch_base_model.py",
         "ml/evaluate_real.py", "ml/requirements.txt", "ml/runpod/train.sh"]

BOOT = ('mkdir -p /workspace/src && echo "$ML_BUNDLE" | base64 -d | tar xz -C /workspace/src '
        '&& bash /workspace/src/ml/runpod/train.sh')


def make_bundle() -> str:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for f in FILES:
            tar.add(ROOT / f, arcname=f)
    return base64.b64encode(buf.getvalue()).decode()


if __name__ == "__main__":
    b = make_bundle()
    print(f"{len(b)} base64 chars")
