#!/usr/bin/env bash
# Runs on the RunPod GPU pod: dataset -> train -> export -> (optional) upload.
# Env: EPOCHS, BACKBONE, INIT_CKPT (default: HF base model; "none" = ImageNet), LR, HF_TOKEN + HF_REPO (to persist results; pods are ephemeral),
#      SETS (default "lea leb 2ed 3ed ced cei"), STOP_POD_WHEN_DONE=1 to stop billing at the end.
set -euo pipefail
cd "$(dirname "$0")/../.."
WORK=${WORK:-/workspace/mtg-engine-run}
mkdir -p "$WORK"
pip install -q -r ml/requirements.txt

# Start from the HF base model unless INIT_CKPT is given (set INIT_CKPT=none for ImageNet init).
if [[ -z "${INIT_CKPT:-}" ]]; then
  INIT_CKPT=$(python ml/fetch_base_model.py --out "$WORK/base" | tail -1)
  LR=${LR:-1e-4}
elif [[ "$INIT_CKPT" == "none" ]]; then
  unset INIT_CKPT
fi

python ml/build_dataset.py --sets ${SETS:-lea leb 2ed 3ed ced cei} --out "$WORK/dataset"
python ml/train.py --data "$WORK/dataset" --out "$WORK/run" --epochs "${EPOCHS:-30}" \
  --backbone "${BACKBONE:-tf_efficientnet_lite0}" --lr "${LR:-3e-4}" ${INIT_CKPT:+--init "$INIT_CKPT"} 2>&1 | tee "$WORK/train.log"
(cd ml && python export.py --ckpt "$WORK/run/best.pt" --data "$WORK/dataset" --out "$WORK/models")
cp "$WORK/run/history.json" "$WORK/train.log" "$WORK/models/"

if [[ -n "${HF_TOKEN:-}" && -n "${HF_REPO:-}" ]]; then
  python - <<PY
from huggingface_hub import HfApi
api = HfApi(token="${HF_TOKEN}")
api.create_repo("${HF_REPO}", private=True, exist_ok=True)
api.upload_folder(folder_path="${WORK}/models", repo_id="${HF_REPO}", commit_message="train run")
print("uploaded to https://huggingface.co/${HF_REPO}")
PY
fi
echo "done: artifacts in $WORK/models"
if [[ "${STOP_POD_WHEN_DONE:-0}" == "1" && -n "${RUNPOD_POD_ID:-}" ]]; then
  runpodctl stop pod "$RUNPOD_POD_ID" || true
fi
