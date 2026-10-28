"""Merge image recognition results into a listing's matches."""
from __future__ import annotations

import logging

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..http import make_client
from ..models import CardPrinting, ImageScan, Listing, Match, utcnow
from .recognizer import Recognition

log = logging.getLogger(__name__)

EDITION_CONFIDENT = 0.6


def load_recognizer(settings: Settings):
    from .recognizer import CardRecognizer

    return CardRecognizer(settings.resolve_path(settings.vision.model_path), settings.resolve_path(settings.vision.gallery_path))


def recognize_url(session: Session, client: httpx.Client, recognizer, url: str) -> list[Recognition] | None:
    """Recognitions for one ad photo, cached per URL and model. None if the photo can't be fetched."""
    cached = session.get(ImageScan, url)
    if cached is not None and cached.model == recognizer.fingerprint:
        return [Recognition(r["card_id"], r["similarity"], r["edition_probs"], None) for r in cached.results]
    try:
        resp = client.get(url)
        resp.raise_for_status()
        recs = recognizer.recognize_image(resp.content)
    except Exception as e:
        log.debug("vision failed on %s: %s", url, e)
        return None
    results = [{"card_id": r.card_id, "similarity": round(r.similarity, 4),
                "edition_probs": {k: round(v, 4) for k, v in r.edition_probs.items()}} for r in recs]
    if cached is None:
        session.add(ImageScan(url=url, model=recognizer.fingerprint, results=results))
    else:
        cached.model, cached.results, cached.scanned_at = recognizer.fingerprint, results, utcnow()
    return [Recognition(r["card_id"], r["similarity"], r["edition_probs"], None) for r in results]


def apply_vision(session: Session, settings: Settings, listing: Listing, recognizer, *,
                 text_evidence: bool = True, client: httpx.Client | None = None) -> list[str]:
    """Recognise cards in the listing photos. Returns extra flags.

    Without text evidence (ads from photo queries like "cartas magic") a photo match needs
    `vision.photo_min_similarity` and a confident target edition, and the ad gets `photo_match`.
    """
    vc = settings.vision
    flags: list[str] = []
    targets = set(settings.games[listing.game].target_codes)
    by_name = {m.card.name: m for m in listing.matches if m.card is not None}
    own_client = client is None
    client = client or make_client(settings)
    try:
        for url in listing.images[: vc.max_images_per_listing]:
            recs = recognize_url(session, client, recognizer, url)
            if recs is None:
                flags.append("photo_unavailable")
                continue
            for r in recs:
                if r.similarity < vc.min_similarity:
                    continue
                card = session.get(CardPrinting, r.card_id)
                if card is None:
                    continue
                edition, p = max(r.edition_probs.items(), key=lambda kv: kv[1])
                if p >= EDITION_CONFIDENT and edition not in targets and edition != "other":
                    if text_evidence:
                        flags.append(f"vision:{edition}")
                    continue
                photo_only = not text_evidence and card.name not in by_name
                if photo_only and (r.similarity < vc.photo_min_similarity or edition not in targets or p < vc.photo_min_edition_prob):
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
                    if existing.source == "vision":
                        existing.confidence = max(existing.confidence, vconf)
                        continue
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
                    if photo_only:
                        flags.append("photo_match")
    finally:
        if own_client:
            client.close()
    return sorted(set(flags))
