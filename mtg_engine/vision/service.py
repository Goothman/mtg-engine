"""Merge image recognition results into a listing's matches."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..http import make_client
from ..models import CardPrinting, ImageScan, Listing, Match, utcnow
from .recognizer import Recognition

log = logging.getLogger(__name__)

EDITION_CONFIDENT = 0.6
IMAGE_TIMEOUT = 15  # seconds per photo; a stalled CDN request must not hold the scan
_downloads = ThreadPoolExecutor(max_workers=8, thread_name_prefix="img")


def load_recognizer(settings: Settings):
    from .recognizer import CardRecognizer

    return CardRecognizer(settings.resolve_path(settings.vision.model_path), settings.resolve_path(settings.vision.gallery_path))


# Why the last attempt on a photo failed ("http_403", "timeout", "decode:image/avif"...), so the
# ad's flags say whether a site refuses image downloads or serves a format we can't read.
_failures: dict[str, str] = {}


def _download(client: httpx.Client, url: str) -> bytes | None:
    try:
        resp = client.get(url, timeout=IMAGE_TIMEOUT)
        resp.raise_for_status()
        _content_types[url] = resp.headers.get("content-type", "?")
        return resp.content
    except httpx.HTTPStatusError as e:
        _failures[url] = f"http_{e.response.status_code}"
    except httpx.TimeoutException:
        _failures[url] = "timeout"
    except Exception as e:
        _failures[url] = type(e).__name__
    log.debug("photo download failed %s: %s", url, _failures[url])
    return None


_content_types: dict[str, str] = {}


def recognize_url(session: Session, client: httpx.Client, recognizer, url: str, content: bytes | None = None,
                  prefetched: bool = False, reuse_cached: bool = False) -> list[Recognition] | None:
    """Recognitions for one ad photo, cached per URL and model. None if the photo can't be fetched.

    reuse_cached: accept a stored reading from any model version (photos never read are still
    downloaded and read).
    """
    cached = session.get(ImageScan, url)
    if cached is not None and (reuse_cached or cached.model == recognizer.fingerprint):
        return [Recognition(r["card_id"], r["similarity"], r["edition_probs"], None) for r in cached.results]
    if not prefetched:
        content = _download(client, url)
    if content is None:
        return None
    try:
        recs = recognizer.recognize_image(content)
    except Exception as e:
        _failures[url] = f"decode:{_content_types.get(url, '?').split(';')[0]}"
        log.debug("vision failed on %s: %s", url, e)
        return None
    results = [{"card_id": r.card_id, "similarity": round(r.similarity, 4),
                "edition_probs": {k: round(v, 4) for k, v in r.edition_probs.items()}} for r in recs]
    if cached is None:
        session.add(ImageScan(url=url, model=recognizer.fingerprint, results=results))
    else:
        cached.model, cached.results, cached.scanned_at = recognizer.fingerprint, results, utcnow()
    return [Recognition(r["card_id"], r["similarity"], r["edition_probs"], None) for r in results]


WHITE_BORDER = ("2ed", "3ed")  # edition classes the model knows with white borders (Unlimited, Revised)


def apply_vision(session: Session, settings: Settings, listing: Listing, recognizer, *,
                 text_evidence: bool = True, strong_text_claim: bool = False,
                 client: httpx.Client | None = None, reuse_cached: bool = False) -> list[str]:
    """Check the listing photos against the text matches and add cards seen only in photos.

    All photo readings are collected first, then:
      * veto: a card confidently seen with a white border in any photo is not Alpha/Beta, whatever
        the text or other photos say. Collectors' Edition is black-bordered and in photos nearly
        identical to Beta (only corners and back differ), so it never vetoes on its own;
      * contradiction: photos were checked but nothing resembles an old card -> text matches demoted,
        unless the text claim is strong (edition in the title, or a graded card in a slab);
      * support: a photo seeing the text-matched card in a target edition raises its confidence;
      * photo-only finds (no supporting text) need `photo_min_similarity` and a confident edition.
    Returns extra flags.
    """
    vc = settings.vision
    flags: list[str] = []
    targets = set(settings.games[listing.game].target_codes)
    by_name = {m.card.name: m for m in listing.matches if m.card is not None}
    own_client = client is None
    client = client or make_client(settings)
    readings: list[tuple[str, Recognition]] = []
    checked = 0
    try:
        # Every photo by default: an unrecognised Alpha/Beta card can be in the 9th photo of a lot.
        urls = listing.images[: vc.max_images_per_listing]
        # download the photos that need reading, in parallel; recognition stays sequential
        todo = [u for u in urls if (c := session.get(ImageScan, u)) is None
                or (not reuse_cached and c.model != recognizer.fingerprint)]
        fetched = dict(zip(todo, _downloads.map(lambda u: _download(client, u), todo)))
        for url in urls:
            recs = recognize_url(session, client, recognizer, url, fetched.get(url), prefetched=url in fetched,
                                 reuse_cached=reuse_cached)
            if recs is None:
                flags += ["photo_unavailable", f"photo_error:{_failures.pop(url, 'unknown')}"]
                continue
            checked += 1
            readings += [(url, r) for r in recs]
    finally:
        if own_client:
            client.close()
    if not checked:
        return sorted(set(flags))

    # 1. vetoes: confident non-target edition for a card name, from any photo
    vetoed: dict[str, str] = {}
    for _, r in readings:
        card = session.get(CardPrinting, r.card_id)
        if card is None or r.similarity < vc.min_similarity:
            continue
        edition, p = max(r.edition_probs.items(), key=lambda kv: kv[1])
        white = sum(r.edition_probs.get(e, 0.0) for e in WHITE_BORDER)
        if white >= vc.white_border_veto:
            vetoed.setdefault(card.name, "white_border")
    for name, why in vetoed.items():
        m = by_name.get(name)
        if m is not None and m.status == "auto":
            m.confidence = round(m.confidence * 0.1, 3)
            flags.append(f"vision:{why}")
        elif m is None and text_evidence:
            flags.append(f"vision:{why}")

    # 2. contradiction: the photos show nothing like any card the model knows
    best = max((r.similarity for _, r in readings), default=0.0)
    if text_evidence and not strong_text_claim and best < vc.photo_contradiction_similarity:
        for m in listing.matches:
            if m.status == "auto" and m.source == "text":
                m.confidence = round(m.confidence * 0.3, 3)
        flags.append("photos_dont_match")
        return sorted(set(flags))

    # 3. support and photo-only finds. A photo-only find must hold across all photos of that card:
    # real modern reprints with the original art pass one lucky crop but not the average.
    ab_by_name: dict[str, list[float]] = {}
    for _, r in readings:
        card = session.get(CardPrinting, r.card_id)
        if card is not None and r.similarity >= vc.min_similarity:
            ab_by_name.setdefault(card.name, []).append(sum(r.edition_probs.get(t, 0.0) for t in targets))
    for url, r in readings:
        if r.similarity < vc.min_similarity:
            continue
        card = session.get(CardPrinting, r.card_id)
        if card is None or card.name in vetoed:
            continue
        photo_only = not text_evidence and card.name not in by_name
        if photo_only:
            # Calibrated on synthetic hard photos (ml/eval_scenes.py): Alpha+Beta probability
            # combined (the model often splits between the two) >= 0.7 with similarity >= 0.7
            # accepted no Unlimited/Revised/Collectors' Edition look-alike.
            p_ab = sum(r.edition_probs.get(t, 0.0) for t in targets)
            agree = ab_by_name.get(card.name, [p_ab])
            if (r.similarity < vc.photo_min_similarity or p_ab < vc.photo_min_edition_prob
                    or sum(agree) / len(agree) < vc.photo_min_edition_prob):
                continue
            edition, p = max(((t, r.edition_probs.get(t, 0.0)) for t in targets), key=lambda kv: kv[1])
            if p < EDITION_CONFIDENT:  # Alpha or Beta, not sure which: value the cheaper printing
                cheapest = session.scalars(select(CardPrinting).where(
                    CardPrinting.name == card.name, CardPrinting.set_code.in_(targets))).all()
                cheapest = min(cheapest, key=lambda c: c.price_eur or 0, default=None)
                if cheapest is not None:
                    card, edition = cheapest, cheapest.set_code
        else:
            edition, p = max(r.edition_probs.items(), key=lambda kv: kv[1])
            if edition not in targets and (p >= EDITION_CONFIDENT or edition != "other"):
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
        if photo_only:
            vconf = round(r.similarity * p_ab, 3)
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
    return sorted(set(flags))
