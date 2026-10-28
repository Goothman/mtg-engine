"""Merge image recognition results into a listing's matches."""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..http import make_client
from ..models import CardPrinting, Listing, Match

log = logging.getLogger(__name__)

EDITION_CONFIDENT = 0.6


def load_recognizer(settings: Settings):
    from .recognizer import CardRecognizer

    return CardRecognizer(settings.resolve_path(settings.vision.model_path), settings.resolve_path(settings.vision.gallery_path))


def apply_vision(session: Session, settings: Settings, listing: Listing, recognizer) -> list[str]:
    """Recognise cards in the listing photos. Returns extra flags."""
    flags: list[str] = []
    targets = set(settings.games[listing.game].target_codes)
    by_name = {m.card.name: m for m in listing.matches if m.card is not None}
    with make_client(settings) as client:
        for url in listing.images[: settings.vision.max_images_per_listing]:
            try:
                img = client.get(url).content
                recs = recognizer.recognize_image(img)
            except Exception as e:
                log.debug("vision failed on %s: %s", url, e)
                continue
            for r in recs:
                if r.similarity < settings.vision.min_similarity:
                    continue
                card = session.get(CardPrinting, r.card_id)
                if card is None:
                    continue
                edition, p = max(r.edition_probs.items(), key=lambda kv: kv[1])
                if p >= EDITION_CONFIDENT and edition not in targets and edition != "other":
                    flags.append(f"vision:{edition}")
                    continue
                # pick the printing of this card name in the predicted target edition
                chosen = card
                if p >= EDITION_CONFIDENT and edition in targets and card.set_code != edition:
                    chosen = session.scalar(
                        select(CardPrinting).where(CardPrinting.name == card.name, CardPrinting.set_code == edition)
                    ) or card
                if chosen.set_code not in targets:
                    continue
                vconf = round(r.similarity * (p if edition in targets else 0.5), 3)
                existing = by_name.get(chosen.name)
                if existing is not None:
                    existing.confidence = round(1 - (1 - existing.confidence) * (1 - vconf), 3)
                    existing.source = "text+vision"
                    if p >= EDITION_CONFIDENT and existing.card_id != chosen.id:
                        existing.card, existing.unit_value_eur, existing.edition_ambiguous = chosen, chosen.price_eur, False
                else:
                    m = Match(card_id=chosen.id, quantity=1, confidence=vconf, source="vision",
                              matched_text=url, unit_value_eur=chosen.price_eur, edition_ambiguous=p < EDITION_CONFIDENT)
                    m.card = chosen
                    listing.matches.append(m)
                    by_name[chosen.name] = m
    return sorted(set(flags))
