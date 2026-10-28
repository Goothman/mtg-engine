# Card recognition model

**Base:** [`dhvazquez/mtg_card_identification_embeddings`](https://huggingface.co/dhvazquez/mtg_card_identification_embeddings)
(MIT). Its layout is `tf_efficientnet_lite0` → BN → Linear (no bias) → BN → 256-d L2-normalised
embedding, with 448×320 input and ImageNet normalisation. It was trained with SubCenterArcFace on
82,115 *printings*, reaching Recall@1 94.1%. Its class map already contains 122 of 295 Alpha and
131 of 302 Beta printings. `CardNet` (`common.py`) uses the same backbone and neck, so the
checkpoint (`backbone_neck`) loads 1:1 (verified: 305/307 tensors; the other 2 are the new edition
head). The RunPod job downloads it automatically (`fetch_base_model.py`).

We fine-tune a multi-task version for the old sets:

| Output | Trained on | Used for |
|---|---|---|
| `embedding` (256-d, L2-norm) | SubCenterArcFace over oracle identities | Which card is it (kNN over `gallery.npz`) |
| `edition_logits` | CE over `lea, leb, 2ed, 3ed, ced, other` | Alpha / Beta / Unlimited / Revised / CE hint |

Training data: Scryfall scans of LEA, LEB, 2ED, 3ED, CED and CEI, with augmentations that
look like marketplace photos (`train.py`: background compositing, perspective, blur,
JPEG artefacts, sleeve glare, fingers, low resolution).

**Limit:** Alpha and Beta share their artwork. They differ mainly in corner radius and small
print details. Treat the edition head as a hint: it lowers or raises confidence, and a
human or the negotiation agent confirms with close-up photos of the corners.

## Pipeline

```bash
pip install -r ml/requirements.txt
python ml/build_dataset.py --out ml/dataset                    # ~1,800 scans
python ml/train.py --data ml/dataset --out ml/runs/v1          # GPU recommended
cd ml && python export.py --ckpt runs/v1/best.pt --data dataset --out ../models
```

Then in `config/default.yaml` set `vision.enabled: true` and run `mtg-engine rematch`.

To start from the HF base model's weights: `python ml/fetch_base_model.py` and pass the printed
path as `--init <file> --lr 1e-4`.

## On RunPod

```bash
pip install runpod
export RUNPOD_API_KEY=... HF_TOKEN=... HF_REPO=<you>/mtg-engine-card-model
python ml/runpod/launch.py --gpu "NVIDIA GeForce RTX 3090" --epochs 30
```

The `ml/` code travels to the pod as plain-text env vars with per-file SHA-256 checksums
(`ml/runpod/bundle.py`), so the private repo needs no git credentials on the pod and a corrupted
file is caught before training starts. The pod runs `ml/runpod/train.sh`
(base model, dataset, train, export), keeps the outputs and `pod.log` under
`/workspace/mtg-engine-run/` on its persistent volume, uploads them to `HF_REPO` if `HF_TOKEN` is
set, and stops itself on success or failure.
Expect about 1–2 GPU-hours for 30 epochs. Download the results into `models/`:

```bash
huggingface-cli download <you>/mtg-engine-card-model --local-dir models
```

## Evaluating on real photos

Label 100–300 real ad photos (`labels.csv`: `path,card_id`) and run
`python ml/evaluate_real.py --labels path/labels.csv`. The training metrics only measure
robustness to synthetic conditions, so real photos are the number that matters.
