"""Scan pipeline: marketplaces -> listings -> matches -> valuation -> events/deals."""
from __future__ import annotations

import logging
import re
from datetime import timedelta

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import events, geo
from .agents.workflow import maybe_close_deal, maybe_open_deal
from .config import Settings
from .db import Database
from .marketplaces import RawListing, get_marketplace
from .marketplaces.milanuncios import BlockedError
from .matching import CardRef, TextMatcher
from .models import CardPrinting, Listing, Match, ScanRun, utcnow
from .text import normalize
from .valuation import value_listing

log = logging.getLogger(__name__)

STALE_AFTER = timedelta(days=7)
GRADED = re.compile(r"\b(psa|cgc|bgs|beckett|gradead[ao]s?|graded|slab)\b")


class _MarketUnreachable(Exception):
    pass
BASIC = {"plains", "island", "swamp", "mountain", "forest"}


def build_matcher(session: Session, settings: Settings, game: str) -> TextMatcher:
    rows = session.scalars(select(CardPrinting).where(CardPrinting.game == game, CardPrinting.is_target.is_(True)))
    refs = [
        CardRef(r.id, r.name, r.name_norm, r.set_code, r.price_eur, r.rarity, r.type_line, r.name_norm in BASIC)
        for r in rows
    ]
    return TextMatcher(refs, settings.games[game])


def ingest(session: Session, settings: Settings, raw: RawListing, game: str) -> tuple[Listing, bool, bool]:
    """Upsert a raw listing. Returns (listing, is_new, content_changed)."""
    row = session.get(Listing, raw.key)
    is_new = row is None
    if is_new:
        row = Listing(id=raw.key, marketplace=raw.marketplace, external_id=raw.external_id, game=game, first_seen_at=utcnow())
        session.add(row)
    changed = is_new or (row.title, row.description, row.price_eur) != (raw.title, raw.description, raw.price_eur)
    lat, lon, precision, province = geo.resolve(raw.country, raw.lat, raw.lon, raw.city, raw.region, raw.key)
    row.url, row.title, row.description, row.price_eur = raw.url, raw.title, raw.description, raw.price_eur
    row.city, row.region, row.postal_code, row.country = raw.city, province or raw.region, raw.postal_code, raw.country
    row.lat, row.lon, row.geo_precision = lat, lon, precision
    if raw.seller_id != row.seller_id:
        row.seller_info = {}
    row.seller_id, row.seller_name = raw.seller_id, raw.seller_name or (row.seller_name if raw.seller_id == row.seller_id else None)
    row.images = raw.images
    row.published_at = raw.published_at or row.published_at
    row.last_seen_at, row.status, row.raw = utcnow(), "active", raw.raw
    return row, is_new, changed


def analyse(session: Session, settings: Settings, listing: Listing, matcher: TextMatcher, recognizer=None,
            image_client=None, keep_vision: bool = False) -> None:
    """(Re)compute matches + valuation for one listing. Manual confirmations/rejections survive.

    keep_vision: re-read the text and revalue (e.g. a known ad changed its price) but keep the
    earlier photo results instead of downloading and recognising the photos again.
    """
    manual = {m.card_id: m for m in listing.matches if m.status != "auto"}
    kept = {m.card_id: m for m in listing.matches if keep_vision and m.status == "auto" and "vision" in (m.source or "")}
    old_photo_flags = [f for f in listing.flags or [] if f.startswith(("photo", "vision:"))] if keep_vision else []
    for m in list(listing.matches):
        if m.status == "auto" and m.card_id not in kept:
            listing.matches.remove(m)
    result = matcher.match(listing.title, listing.description)
    for tm in result.matches:
        if tm.card.id in manual or tm.card.id in kept:
            continue
        listing.matches.append(
            Match(
                card=session.get(CardPrinting, tm.card.id),
                quantity=tm.quantity,
                confidence=tm.confidence,
                source="text",
                matched_text=tm.matched_text,
                unit_value_eur=tm.card.price_eur,
                edition_ambiguous=tm.edition_ambiguous,
            )
        )
    flags = list(result.flags) + old_photo_flags
    excluded = sorted({normalize(f) for f in settings.games[listing.game].exclude_flags} & set(flags))
    # Vision on candidates (an edition named or a card matched) and on every ad found by a
    # photo query; image downloads are the expensive part, and results are cached per photo.
    text_evidence = bool(result.editions or result.matches)
    if recognizer is not None and not keep_vision and listing.images and (text_evidence or listing.photo_scan) and not excluded:
        from .vision.service import apply_vision

        # The "photos don't match" check is only trusted when the edition claim is weak: not in
        # the title (e.g. "Myr alfa" in a card list) and not a graded card (slabs photograph badly).
        strong_claim = bool(matcher.match(listing.title, "").editions) or bool(GRADED.search(normalize(f"{listing.title} {listing.description}")))
        flags += apply_vision(session, settings, listing, recognizer, text_evidence=text_evidence,
                              strong_text_claim=strong_claim, client=image_client)
    listing.editions = result.editions
    if excluded:  # e.g. "coreano": not an English Alpha/Beta card, whatever the photos look like
        for m in listing.matches:
            if m.status == "auto":
                m.confidence = 0.0
        flags.append("excluded")
    # No penalty for a price far below the cards' value: those are the bargains we're looking for.
    v = value_listing(listing.matches, listing.price_eur)
    listing.flags = flags
    listing.est_total_value, listing.est_score, listing.value_ratio = v.total_value, v.score, v.ratio
    listing.match_count = v.counted
    # photo-only finds are shown for a human to check even when below the counting threshold
    listing.needs_review = (result.needs_review or "photo_match" in flags) and v.counted == 0


class Scanner:
    def __init__(self, settings: Settings, db: Database, recognizer=None):
        self.settings = settings
        self.db = db
        self.recognizer = recognizer
        self.image_client = None
        self._sellers: dict[str, dict] = {}

    def run(self, games: list[str] | None = None, marketplaces: list[str] | None = None, max_queries: int | None = None,
            photo_queries: bool | None = None) -> dict:
        """photo_queries: None = text + photo queries, True = photo queries only, False = text only."""
        stats = {"fetched": 0, "new": 0, "updated": 0, "matched": 0, "photo_checked": 0, "deals_opened": 0, "errors": 0}
        if self.recognizer is not None:
            from .http import make_client

            self.image_client = make_client(self.settings)
        with self.db.session() as s:
            run = ScanRun()
            s.add(run)
            s.flush()
            run_id = run.id
        errors: list[str] = []
        for game, gcfg in self.settings.enabled_games().items():
            if games and game not in games:
                continue
            for mname, mcfg in self.settings.enabled_marketplaces().items():
                if marketplaces and mname not in marketplaces:
                    continue
                market = get_marketplace(mname, self.settings)
                points = self.settings.region.search_points if mcfg.use_search_points else [None]
                try:
                    # Photo (generic) queries go first: on a marketplace with a paid result budget they
                    # are the valuable ones (unrecognised Alpha/Beta cards in "cartas magic" lots).
                    queries = []
                    if photo_queries is not False and self.recognizer is not None:  # useless without vision
                        queries += [(q, True) for q in gcfg.photo_queries[:max_queries]]
                    if photo_queries is not True:
                        queries += [(q, False) for q in gcfg.queries[:max_queries]]
                    queries = [(q, p) for q, p in queries if ("photo" if p else "text") in mcfg.query_kinds]
                    for query, photo in queries:
                        for point in points:
                            try:
                                self._scan_query(market, game, query, point, stats, photo)
                            except (httpx.ProxyError, httpx.ConnectError, BlockedError) as e:
                                raise _MarketUnreachable(f"{mname} unreachable: {type(e).__name__}: {e}") from e
                            except Exception as e:
                                stats["errors"] += 1
                                errors.append(f"{mname} {query!r} {getattr(point, 'name', '')}: {type(e).__name__}: {e}")
                                log.warning(errors[-1])
                except _MarketUnreachable as e:  # don't hammer a host we can't reach; skip it this run
                    stats["errors"] += 1
                    errors.append(str(e))
                    log.error("%s; skipping %s for the rest of this scan", e, mname)
                finally:
                    market.close()
        if self.image_client is not None:
            self.image_client.close()
        with self.db.session() as s:
            self._expire_stale(s)
            run = s.get(ScanRun, run_id)
            run.finished_at, run.stats, run.errors = utcnow(), stats, errors[-50:]
        return stats

    def _scan_query(self, market, game, query, point, stats, photo: bool = False) -> None:
        log.info("query %r @ %s%s | so far %s", query, getattr(point, "name", "-"), " (photo)" if photo else "", stats)
        seen: set[str] = set()
        with self.db.session() as s:
            matcher = build_matcher(s, self.settings, game)
            for raw in market.search(query, point):
                if raw.key in seen:
                    continue
                seen.add(raw.key)
                stats["fetched"] += 1
                listing, is_new, changed = ingest(s, self.settings, raw, game)
                newly_photo = photo and not listing.photo_scan
                listing.photo_scan = listing.photo_scan or photo
                photo_now = newly_photo and self.recognizer is not None  # never photo-checked before
                if not (is_new or changed or photo_now):
                    continue
                s.flush()
                # Incremental: new ads (and ads never photo-checked) get the full analysis; known ads
                # only get their text re-read and value recalculated, keeping earlier photo results.
                analyse(s, self.settings, listing, matcher, self.recognizer, self.image_client,
                        keep_vision=not (is_new or photo_now))
                if (is_new or photo_now) and listing.photo_scan and self.recognizer is not None and listing.images:
                    stats["photo_checked"] += 1
                    if stats["photo_checked"] % 100 == 0:
                        log.info("photo-checked %d ads so far (%d matched)", stats["photo_checked"], stats["matched"])
                if listing.match_count or listing.needs_review:
                    enrich_seller(s, market, listing, self._sellers)
                if is_new:
                    stats["new"] += 1
                    events.emit(s, events.LISTING_DISCOVERED, listing.id, marketplace=listing.marketplace)
                elif changed:  # (an unchanged ad may still be re-analysed for its first photo check)
                    stats["updated"] += 1
                    events.emit(s, events.LISTING_UPDATED, listing.id, price=listing.price_eur)
                if listing.match_count:
                    stats["matched"] += 1
                    events.emit(s, events.LISTING_MATCHED, listing.id, value=listing.est_total_value, ratio=listing.value_ratio, price=listing.price_eur)
                    if maybe_open_deal(s, self.settings, listing):
                        stats["deals_opened"] += 1
                else:
                    maybe_close_deal(s, listing)
                s.flush()

    def _expire_stale(self, s: Session) -> None:
        cutoff = utcnow() - STALE_AFTER
        stale = list(s.scalars(select(Listing.id).where(Listing.status == "active", Listing.last_seen_at < cutoff, Listing.is_demo.is_(False))))
        if stale:
            s.execute(update(Listing).where(Listing.id.in_(stale)).values(status="gone"))
            for lid in stale:
                events.emit(s, events.LISTING_GONE, lid)


def enrich_seller(session: Session, market, listing: Listing, cache: dict[str, dict] | None = None) -> None:
    """Fetch the seller's public profile (name, rating, sales) for ads worth showing."""
    if not listing.seller_id or listing.seller_info:
        return
    cache = {} if cache is None else cache
    info = cache.get(listing.seller_id)
    if info is None:
        known = session.scalars(select(Listing.seller_info).where(
            Listing.seller_id == listing.seller_id, Listing.marketplace == listing.marketplace, Listing.id != listing.id))
        info = next((k for k in known if k), None)
    if info is None:
        try:
            info = market.seller_profile(listing.seller_id) or {}
        except Exception as e:
            log.warning("seller profile %s failed: %s", listing.seller_id, e)
            return
    cache[listing.seller_id] = info
    listing.seller_info = info
    listing.seller_name = info.get("name") or listing.seller_name


def enrich_sellers(settings: Settings, db: Database) -> int:
    """Fill seller profiles for ads already in the database that are shown on the dashboard."""
    n = 0
    cache: dict[str, dict] = {}
    with db.session() as s:
        rows = list(s.scalars(select(Listing).where(
            Listing.status == "active", Listing.is_demo.is_(False),
            (Listing.match_count > 0) | Listing.needs_review.is_(True))))
        markets = {}
        for listing in rows:
            if listing.seller_info or listing.marketplace not in settings.marketplaces:
                continue
            market = markets.get(listing.marketplace) or markets.setdefault(
                listing.marketplace, get_marketplace(listing.marketplace, settings))
            enrich_seller(s, market, listing, cache)
            n += bool(listing.seller_info)
        for m in markets.values():
            m.close()
    return n


def rematch_all(settings: Settings, db: Database, recognizer=None, max_minutes: float | None = None,
                batch: int = 100, keep_photos: bool = False) -> int:
    """Re-run matching/valuation on stored listings (after a price, config or model change).

    With vision, a new model or detector means re-downloading every photo, which may not fit in
    one run: ads are processed by priority (text evidence first, then ads from photo queries,
    newest first), committed in batches, and the run stops at `max_minutes`; the next run
    redoes the rest (photos already re-read hit the cache and are fast).
    keep_photos: apply the current text/valuation rules but keep stored photo results (fast).
    """
    import time

    deadline = time.monotonic() + max_minutes * 60 if max_minutes else None
    with db.session() as s:
        rows = s.execute(select(Listing.id, Listing.match_count, Listing.needs_review, Listing.editions,
                                Listing.photo_scan, Listing.last_seen_at).where(Listing.status == "active")).all()
    order = sorted(rows, key=lambda r: (not (r.match_count or r.needs_review or r.editions), not r.photo_scan,
                                         -(r.last_seen_at.timestamp() if r.last_seen_at else 0)))
    ids = [r.id for r in order]
    n = 0
    client = None
    if recognizer is not None:
        from .http import make_client

        client = make_client(settings)
    try:
        for start in range(0, len(ids), batch):
            if deadline and time.monotonic() > deadline:
                log.warning("rematch stopped at the time limit after %d/%d ads; the next run continues", n, len(ids))
                break
            with db.session() as s:  # one commit per batch keeps progress
                matchers: dict[str, TextMatcher] = {}
                for listing in s.scalars(select(Listing).where(Listing.id.in_(ids[start : start + batch]))):
                    if listing.game not in settings.games:
                        continue
                    m = matchers.get(listing.game) or matchers.setdefault(listing.game, build_matcher(s, settings, listing.game))
                    analyse(s, settings, listing, m, recognizer, client, keep_vision=keep_photos)
                    if listing.match_count:
                        maybe_open_deal(s, settings, listing)
                    else:
                        maybe_close_deal(s, listing)
                    n += 1
            if n and n % 1000 < batch:
                log.info("rematch: %d/%d ads", n, len(ids))
    finally:
        if client is not None:
            client.close()
    return n
