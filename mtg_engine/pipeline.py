"""Scan pipeline: marketplaces -> listings -> matches -> valuation -> events/deals."""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import events, geo
from .agents.workflow import maybe_open_deal
from .config import Settings
from .db import Database
from .marketplaces import RawListing, get_marketplace
from .matching import CardRef, TextMatcher
from .models import CardPrinting, Listing, Match, ScanRun, utcnow
from .valuation import value_listing

log = logging.getLogger(__name__)

STALE_AFTER = timedelta(days=7)
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
    row.seller_id, row.seller_name, row.images = raw.seller_id, raw.seller_name, raw.images
    row.published_at = raw.published_at or row.published_at
    row.last_seen_at, row.status, row.raw = utcnow(), "active", raw.raw
    return row, is_new, changed


def analyse(session: Session, settings: Settings, listing: Listing, matcher: TextMatcher, recognizer=None) -> None:
    """(Re)compute matches + valuation for one listing. Manual confirmations/rejections survive."""
    manual = {m.card_id: m for m in listing.matches if m.status != "auto"}
    for m in list(listing.matches):
        if m.status == "auto":
            listing.matches.remove(m)
    result = matcher.match(listing.title, listing.description)
    for tm in result.matches:
        if tm.card.id in manual:
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
    flags = list(result.flags)
    if recognizer is not None and listing.images:
        from .vision.service import apply_vision

        flags += apply_vision(session, settings, listing, recognizer)
    listing.editions = result.editions
    listing.flags = flags
    v = value_listing(listing.matches, listing.price_eur)
    listing.est_total_value, listing.est_score, listing.value_ratio = v.total_value, v.score, v.ratio
    listing.match_count = v.counted
    listing.needs_review = result.needs_review and v.counted == 0


class Scanner:
    def __init__(self, settings: Settings, db: Database, recognizer=None):
        self.settings = settings
        self.db = db
        self.recognizer = recognizer

    def run(self, games: list[str] | None = None, marketplaces: list[str] | None = None, max_queries: int | None = None) -> dict:
        stats = {"fetched": 0, "new": 0, "updated": 0, "matched": 0, "deals_opened": 0, "errors": 0}
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
                    for query in gcfg.queries[:max_queries]:
                        for point in points:
                            try:
                                self._scan_query(market, game, query, point, stats)
                            except Exception as e:
                                stats["errors"] += 1
                                errors.append(f"{mname} {query!r} {getattr(point, 'name', '')}: {type(e).__name__}: {e}")
                                log.warning(errors[-1])
                finally:
                    market.close()
        with self.db.session() as s:
            self._expire_stale(s)
            run = s.get(ScanRun, run_id)
            run.finished_at, run.stats, run.errors = utcnow(), stats, errors[-50:]
        return stats

    def _scan_query(self, market, game, query, point, stats) -> None:
        seen: set[str] = set()
        with self.db.session() as s:
            matcher = build_matcher(s, self.settings, game)
            for raw in market.search(query, point):
                if raw.key in seen:
                    continue
                seen.add(raw.key)
                stats["fetched"] += 1
                listing, is_new, changed = ingest(s, self.settings, raw, game)
                if not (is_new or changed):
                    continue
                s.flush()
                analyse(s, self.settings, listing, matcher, self.recognizer)
                stats["new" if is_new else "updated"] += 1
                if is_new:
                    events.emit(s, events.LISTING_DISCOVERED, listing.id, marketplace=listing.marketplace)
                else:
                    events.emit(s, events.LISTING_UPDATED, listing.id, price=listing.price_eur)
                if listing.match_count:
                    stats["matched"] += 1
                    events.emit(s, events.LISTING_MATCHED, listing.id, value=listing.est_total_value, ratio=listing.value_ratio, price=listing.price_eur)
                    if maybe_open_deal(s, self.settings, listing):
                        stats["deals_opened"] += 1
                s.flush()

    def _expire_stale(self, s: Session) -> None:
        cutoff = utcnow() - STALE_AFTER
        stale = list(s.scalars(select(Listing.id).where(Listing.status == "active", Listing.last_seen_at < cutoff, Listing.is_demo.is_(False))))
        if stale:
            s.execute(update(Listing).where(Listing.id.in_(stale)).values(status="gone"))
            for lid in stale:
                events.emit(s, events.LISTING_GONE, lid)


def rematch_all(settings: Settings, db: Database, recognizer=None) -> int:
    """Re-run matching/valuation on stored listings (after a price or config change)."""
    n = 0
    with db.session() as s:
        matchers: dict[str, TextMatcher] = {}
        for listing in s.scalars(select(Listing).where(Listing.status == "active")):
            if listing.game not in settings.games:
                continue
            m = matchers.get(listing.game) or matchers.setdefault(listing.game, build_matcher(s, settings, listing.game))
            analyse(s, settings, listing, m, recognizer)
            if listing.match_count:
                maybe_open_deal(s, settings, listing)
            n += 1
    return n
