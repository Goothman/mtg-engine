"""Ship ml/ to a RunPod pod through environment variables (no git credentials needed).

Each file travels as plain text in its own env var (F_*), with a 16-hex SHA-256 in
F_*_SHA. The pod's boot command (BOOT) writes the files, verifies every checksum and
refuses to train on a mismatch. Plain text, not one base64 blob: a single corrupted
character in a 13 KB blob broke a run; per-file checksums pinpoint the bad file.

RunPod gotchas (seen live):
  * trailing newlines in env values are trimmed  -> we hash content.strip() + "\\n"
  * "$RUNPOD_POD_ID" inside env values is expanded to the pod id -> BOOT maps it back
    before hashing.

    python ml/runpod/bundle.py > /tmp/pod_env.json   # {"env": {...}, "cmd": "..."}
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILES = {
    "F_COMMON": "ml/common.py",
    "F_TRAIN": "ml/train.py",
    "F_EXPORT": "ml/export.py",
    "F_DATASET": "ml/build_dataset.py",
    "F_FETCH": "ml/fetch_base_model.py",
    "F_REQS": "ml/requirements.txt",
    "F_TRAINSH": "ml/runpod/train.sh",
}

BOOT = (
    "mkdir -p /workspace/src/ml/runpod && cd /workspace/src && python3 -c \"import os,hashlib,sys\n"
    f"m={json.dumps(FILES).replace(chr(34), chr(39))}\n"
    "pid=os.environ.get('RUNPOD_POD_ID','')\n"
    "bad=0\n"
    "for k,p in m.items():\n"
    "    s=os.environ[k].strip()+'\\n'\n"
    "    open(p,'w').write(s)\n"
    "    canon=s.replace(pid,'$'+'RUNPOD_POD_ID') if pid else s\n"
    "    h=hashlib.sha256(canon.encode()).hexdigest()[:16]\n"
    "    ok=h==os.environ[k+'_SHA']\n"
    "    bad+=not ok\n"
    "    print('verify',p,h,'OK' if ok else 'MISMATCH expected '+os.environ[k+'_SHA'])\n"
    "sys.exit(1 if bad else 0)\" && bash ml/runpod/train.sh || sleep infinity"
)


def _strip_py(src: str) -> str:
    """Drop comments and docstrings to keep env vars small (behaviour unchanged)."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) and isinstance(first.value.value, str):
                node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree) + "\n"


def _strip_other(src: str) -> str:
    return "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#") or l.startswith("#!")) + "\n"


def make_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for key, rel in FILES.items():
        src = (ROOT / rel).read_text()
        text = (_strip_py(src) if rel.endswith(".py") else _strip_other(src)).strip() + "\n"
        env[key] = text
        env[key + "_SHA"] = hashlib.sha256(text.encode()).hexdigest()[:16]
    return env


if __name__ == "__main__":
    print(json.dumps({"env": make_env(), "cmd": BOOT}, indent=1))
