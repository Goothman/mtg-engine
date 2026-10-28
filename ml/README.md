# Card recognition model

**Base:** [`dhvazquez/mtg_card_identification_embeddings`](https://huggingface.co/dhvazquez/mtg_card_identification_embeddings),
EfficientNet-Lite0 + SubCenterArcFace, image → embedding, reported Recall@1 94.1%.
We keep its approach (metric learning + gallery retrieval) and fine-tune a multi-task version
for the old sets:

| Output | Trained on | Used for |
|---|---|---|
| `embedding` (512-d, L2-norm) | SubCenterArcFace over oracle identities | Which card is it (kNN over `gallery.npz`) |
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

To start from the HF base model's weights, download its checkpoint and pass
`--init <file> --backbone <its timm name>`. The script prints how many tensors matched.

## On RunPod

```bash
pip install runpod
export RUNPOD_API_KEY=... HF_TOKEN=... HF_REPO=<you>/mtg-engine-card-model
python ml/runpod/launch.py --gpu "NVIDIA GeForce RTX 4090" --epochs 30
```

The pod clones the repo and runs `ml/runpod/train.sh` (dataset, train, export). It uploads
`card_model.onnx`, `gallery.npz` and the logs to your private HF repo, then stops itself.
Expect about 1–2 GPU-hours for 30 epochs. Download the results into `models/`:

```bash
huggingface-cli download <you>/mtg-engine-card-model --local-dir models
```

## Evaluating on real photos

Label 100–300 real ad photos (`labels.csv`: `path,card_id`) and run
`python ml/evaluate_real.py --labels path/labels.csv`. The training metrics only measure
robustness to synthetic conditions, so real photos are the number that matters.
