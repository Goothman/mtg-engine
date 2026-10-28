"""Launch a one-off RunPod GPU pod that clones this repo and runs ml/runpod/train.sh.

    pip install runpod
    export RUNPOD_API_KEY=...            # https://www.runpod.io/console/user/settings
    export HF_TOKEN=... HF_REPO=you/mtg-engine-card-model   # where results are uploaded
    export GITHUB_TOKEN=...              # only if the repo is private
    python ml/runpod/launch.py --gpu "NVIDIA GeForce RTX 4090" --epochs 30

Cost: billed per second while the pod runs (an RTX 4090 community pod is roughly
$0.3-0.7/h; check the console for current prices). The script sets
STOP_POD_WHEN_DONE=1, so the pod stops itself after uploading; stopped pods still
bill for their volume until you terminate them.
"""
from __future__ import annotations

import argparse
import os

import runpod

IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gpu", default="NVIDIA GeForce RTX 4090")
    ap.add_argument("--cloud", default="ALL", choices=["ALL", "SECURE", "COMMUNITY"])
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--repo", default="https://github.com/Goothman/mtg-engine.git")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--backbone", default="tf_efficientnet_lite0")
    args = ap.parse_args()

    runpod.api_key = os.environ["RUNPOD_API_KEY"]
    repo = args.repo
    if tok := os.environ.get("GITHUB_TOKEN"):
        repo = repo.replace("https://", f"https://x-access-token:{tok}@")
    cmd = (
        f"bash -c 'git clone --depth 1 -b {args.branch} {repo} /workspace/mtg-engine && "
        f"bash /workspace/mtg-engine/ml/runpod/train.sh; sleep infinity'"
    )
    env = {"EPOCHS": str(args.epochs), "BACKBONE": args.backbone, "STOP_POD_WHEN_DONE": "1"}
    for k in ("HF_TOKEN", "HF_REPO"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    if "HF_REPO" not in env:
        print("warning: HF_REPO not set; trained weights stay on the pod volume only")
    pod = runpod.create_pod(
        name="mtg-engine-train", image_name=IMAGE, gpu_type_id=args.gpu, cloud_type=args.cloud, gpu_count=1,
        container_disk_in_gb=40, volume_in_gb=20, volume_mount_path="/workspace", env=env, docker_args=cmd,
        ports="22/tcp",
    )
    print(f"pod {pod['id']} created; follow logs in the RunPod console")


if __name__ == "__main__":
    main()
