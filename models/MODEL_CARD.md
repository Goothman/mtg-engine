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
