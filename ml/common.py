"""Shared model + preprocessing for training, export and gallery building."""
from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F

INPUT_H, INPUT_W = 448, 320  # same as the HF base model (dhvazquez), portrait card
MEAN = (0.485, 0.456, 0.406)
STD = (0.229, 0.224, 0.225)
# Edition head classes. cei (Intl. Collectors' Ed.) shares CE's square corners -> "ced".
EDITION_CLASSES = ["lea", "leb", "2ed", "3ed", "ced", "other"]
SET_TO_EDITION = {"lea": "lea", "leb": "leb", "2ed": "2ed", "3ed": "3ed", "ced": "ced", "cei": "ced"}


def edition_index(set_code: str) -> int:
    return EDITION_CLASSES.index(SET_TO_EDITION.get(set_code, "other"))


class CardNet(nn.Module):
    """Backbone -> neck -> L2-normalised identity embedding, plus an edition head.

    `backbone` + `neck` mirror dhvazquez/mtg_card_identification_embeddings exactly
    (tf_efficientnet_lite0 -> BN -> Linear(no bias) -> BN, 256-d), so its `backbone_neck`
    checkpoint loads 1:1. The edition head is new and trained from scratch.
    """

    def __init__(self, backbone: str = "tf_efficientnet_lite0", embed_dim: int = 256, pretrained: bool = True):
        super().__init__()
        import timm

        self.backbone = timm.create_model(backbone, pretrained=pretrained, num_classes=0, global_pool="avg")
        feat = self.backbone.num_features
        self.neck = nn.Sequential(nn.BatchNorm1d(feat), nn.Linear(feat, embed_dim, bias=False), nn.BatchNorm1d(embed_dim))
        self.edition = nn.Sequential(nn.Dropout(0.3), nn.Linear(feat, len(EDITION_CLASSES)))

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        f = self.backbone(x)
        return F.normalize(self.neck(f), dim=1), self.edition(f)
