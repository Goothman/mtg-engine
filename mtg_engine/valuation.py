"""Listing valuation and ranking."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

COUNTED_CONFIDENCE = 0.5  # matches below this are shown for review but not added to the total


class Valued(Protocol):
    quantity: int
    confidence: float
    unit_value_eur: float | None
    status: str


@dataclass
class Valuation:
    total_value: float
    score: float
    ratio: float | None
    counted: int


def value_listing(matches: list[Valued], asking_price: float | None) -> Valuation:
    total = score = 0.0
    counted = 0
    for m in matches:
        if m.status == "rejected" or not m.unit_value_eur:
            continue
        conf = 1.0 if m.status == "confirmed" else m.confidence
        if conf < COUNTED_CONFIDENCE:
            continue
        line = m.quantity * m.unit_value_eur
        total += line
        score += conf * line
        counted += 1
    ratio = round(total / asking_price, 3) if asking_price and asking_price > 0 and total else None
    return Valuation(round(total, 2), round(score, 2), ratio, counted)
