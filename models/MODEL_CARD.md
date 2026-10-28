# Card recognition model v1 (Alpha/Beta)

Card recognition model fine-tuned for Magic: The Gathering Alpha/Beta.

- Base: dhvazquez/mtg_card_identification_embeddings (MIT); its backbone and neck were loaded 1:1 (305/307 tensors)
- Trained on RunPod (RTX 5080), 12 epochs on 1,809 Scryfall scans (LEA, LEB, 2ED, 3ED, CED, CEI) with augmentations that imitate marketplace photos
- Outputs: 256-d identity embedding (kNN over gallery.npz) and edition logits (lea, leb, 2ed, 3ed, ced, other)

Final validation (fixed-seed augmented views of the training scans): card recall@1 1.00, edition accuracy 0.86
(LEA 0.66, LEB 0.69, 2ED 0.93, 3ED 0.98, CE 0.95). Local check through the production recognizer on 30 cards as
simulated photos: name 30/30, exact printing 26/30, edition 29/30.
These numbers come from seen printings in synthetic conditions. Accuracy on real seller photos still needs a labelled set (ROADMAP Phase 2).

Files: `card_model.onnx` (ONNX, input 1x3x448x320, outputs `embedding`, `edition_logits`), `gallery.npz` (embeddings + preprocessing metadata), `history.json` (per-epoch metrics). SHA-256 prefixes: card_model.onnx 37fa97943909f091, gallery.npz 2c2691fdc0e397fb.

## Hard-photo evaluation (2026-10-28)

960 synthetic ad photos (`ml/make_hard_scenes.py`, fixed seed): Alpha/Beta cards and look-alikes
(Unlimited, Revised, Collectors' Edition) upside down, sideways, partly covered by another card, in piles,
cut off by the frame, with glare and in sleeves. Scored with `ml/eval_scenes.py` (full pipeline: detection + model).
A = this model with the old detector, B = this model with detector v2 (deployed: sliding windows, upside-down and
glare-removed views), C = a retrained v2 model (extra occlusion/rotation/glare augmentations).

| Situation | Found A | B | C | Edition right A | B | C |
|---|---|---|---|---|---|---|
| photo | 93% | 99% | 100% | 64% | 67% | 70% |
| upside_down | 13% | 99% | 100% | 6% | 81% | 68% |
| sideways | 59% | 99% | 99% | 49% | 78% | 63% |
| covered | 1% | 60% | 87% | 1% | 21% | 36% |
| pile | 3% | 54% | 77% | 3% | 26% | 38% |
| cut_off | 0% | 11% | 10% | 0% | 4% | 2% |
| glare | 81% | 98% | 98% | 63% | 72% | 63% |
| sleeve | 73% | 98% | 99% | 57% | 64% | 54% |

C finds more covered/piled cards but confuses Collectors' Edition with Alpha/Beta: at any threshold with useful
recall it accepted 10-43 of 111 CE look-alikes as Alpha/Beta. So this model (v1) stays, with detector v2.
Photo-only finds (no supporting text) need similarity >= 0.7 and Alpha+Beta probability >= 0.7: on 240 Alpha/Beta
and 240 look-alike photos that accepted 42% of the Alpha/Beta cards and none of the look-alikes. The white-border
veto (Unlimited+Revised probability >= 0.5) rules out 88% of white-bordered look-alikes and wrongly 2% of Alpha/Beta.
Still synthetic: real seller photos need a labelled set. Full numbers: `ml/eval/hard_scenes_2026-10-28.json`.

