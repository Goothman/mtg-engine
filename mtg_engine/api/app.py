"""HTTP API + dashboard. Also the integration surface for external agents."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import events
from ..agents import STAGES, TRANSITIONS, InvalidTransition, transition
from ..agents.workflow import add_note
from ..config import Settings, get_settings
from ..db import Database
from ..models import CardPrinting, Deal, Event, Listing, Match, ScanRun
from ..valuation import value_listing

WEB = Path(__file__).resolve().parent.parent / "web"


# ---- serialisers ---------------------------------------------------------------------

def match_out(m: Match) -> dict[str, Any]:
    c = m.card
    return {
        "id": m.id, "card_id": m.card_id, "name": c.name if c else None, "set_code": c.set_code if c else None,
        "set_name": c.set_name if c else None, "rarity": c.rarity if c else None, "image_url": c.image_url if c else None,
        "quantity": m.quantity, "confidence": m.confidence, "source": m.source, "matched_text": m.matched_text,
        "unit_value_eur": m.unit_value_eur, "line_value_eur": round((m.unit_value_eur or 0) * m.quantity, 2),
        "price_source": c.price_source if c else None, "edition_ambiguous": m.edition_ambiguous, "status": m.status,
    }


def listing_out(l: Listing, with_matches: bool = True) -> dict[str, Any]:
    d = {
        "id": l.id, "marketplace": l.marketplace, "game": l.game, "url": l.url, "title": l.title,
        "description": l.description, "price_eur": l.price_eur, "city": l.city, "region": l.region,
        "lat": l.lat, "lon": l.lon, "geo_precision": l.geo_precision, "seller_name": l.seller_name,
        "seller_id": l.seller_id, "seller": l.seller_info or {}, "photo_scan": l.photo_scan,
        "images": l.images, "published_at": l.published_at, "first_seen_at": l.first_seen_at,
        "last_seen_at": l.last_seen_at, "status": l.status, "is_demo": l.is_demo,
        "est_total_value": l.est_total_value, "est_score": l.est_score, "value_ratio": l.value_ratio,
        "match_count": l.match_count, "editions": l.editions, "flags": l.flags, "needs_review": l.needs_review,
        "deal": deal_out(l.deal, with_listing=False) if l.deal else None,
    }
    if with_matches:
        d["matches"] = [match_out(m) for m in sorted(l.matches, key=lambda m: -(m.unit_value_eur or 0) * m.quantity)]
    return d


def deal_out(d: Deal, with_listing: bool = True) -> dict[str, Any]:
    out = {
        "id": d.id, "listing_id": d.listing_id, "stage": d.stage, "assigned_agent": d.assigned_agent,
        "offer_price_eur": d.offer_price_eur, "agreed_price_eur": d.agreed_price_eur, "shipping": d.shipping,
        "notes": d.notes, "drafts": d.drafts, "created_at": d.created_at, "updated_at": d.updated_at,
        "allowed_next": sorted(TRANSITIONS.get(d.stage, set())),
    }
    if with_listing:
        out["listing"] = listing_out(d.listing, with_matches=True)
    return out


def event_out(e: Event) -> dict[str, Any]:
    return {"id": e.id, "type": e.type, "subject_id": e.subject_id, "payload": e.payload, "created_at": e.created_at,
            "attempts": e.attempts, "claimed_by": e.claimed_by}


# ---- request bodies ------------------------------------------------------------------

class MatchReview(BaseModel):
    status: str  # confirmed | rejected | auto


class TransitionIn(BaseModel):
    to: str
    by: str = "human"
    note: str | None = None
    fields: dict[str, Any] = {}


class NoteIn(BaseModel):
    by: str = "human"
    text: str


class AckIn(BaseModel):
    error: str | None = None


class ScanIn(BaseModel):
    games: list[str] | None = None
    marketplaces: list[str] | None = None
    max_queries: int | None = None


# ---- app -----------------------------------------------------------------------------

def create_app(settings: Settings | None = None, db: Database | None = None) -> FastAPI:
    settings = settings or get_settings()
    db = db or Database(settings)
    db.create_all()
    app = FastAPI(title="mtg-engine", version="0.1.0")
    app.state.settings, app.state.db = settings, db
    scan_lock = threading.Lock()

    def session():
        with db.session() as s:
            yield s

    S = Depends(session)

    @app.get("/api/config")
    def config_view():
        return {
            "map_center": settings.region.map_center, "map_zoom": settings.region.map_zoom,
            "country": settings.region.country, "stages": STAGES,
            "marketplaces": list(settings.enabled_marketplaces()),
            "games": {k: {"targets": [t.model_dump(include={"set_code", "label"}) for t in g.targets]} for k, g in settings.enabled_games().items()},
            "vision_enabled": settings.vision.enabled,
            "map": settings.map.model_dump(),
        }

    @app.get("/api/stats")
    def stats(s: Session = S):
        active = Listing.status == "active"
        matched = active & (Listing.match_count > 0)
        last = s.scalar(select(ScanRun).order_by(ScanRun.id.desc()).limit(1))
        by_market = dict(s.execute(select(Listing.marketplace, func.count()).where(matched).group_by(Listing.marketplace)).all())
        stage_counts = dict(s.execute(select(Deal.stage, func.count()).group_by(Deal.stage)).all())
        return {
            "listings_active": s.scalar(select(func.count()).where(active)),
            "listings_matched": s.scalar(select(func.count()).where(matched)),
            "needs_review": s.scalar(select(func.count()).where(active & Listing.needs_review.is_(True))),
            "total_value_eur": round(s.scalar(select(func.coalesce(func.sum(Listing.est_total_value), 0)).where(matched)) or 0, 2),
            "total_asking_eur": round(s.scalar(select(func.coalesce(func.sum(Listing.price_eur), 0)).where(matched)) or 0, 2),
            "best_ratio": s.scalar(select(func.max(Listing.value_ratio)).where(matched)),
            "cards_in_catalog": s.scalar(select(func.count()).select_from(CardPrinting)),
            "matched_by_marketplace": by_market,
            "deals_by_stage": stage_counts,
            "last_scan": {"started_at": last.started_at, "finished_at": last.finished_at, "stats": last.stats, "errors": last.errors[:5]} if last else None,
        }

    def _filtered(game, marketplace, edition, min_value, min_ratio, q, status, include_unmatched, review):
        stmt = select(Listing)
        if status != "all":
            stmt = stmt.where(Listing.status == status)
        if not include_unmatched:
            stmt = stmt.where(or_(Listing.match_count > 0, Listing.needs_review.is_(True)) if review else Listing.match_count > 0)
        if game:
            stmt = stmt.where(Listing.game == game)
        if marketplace:
            stmt = stmt.where(Listing.marketplace == marketplace)
        if min_value:
            stmt = stmt.where(Listing.est_total_value >= min_value)
        if min_ratio:
            stmt = stmt.where(Listing.value_ratio >= min_ratio)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(or_(Listing.title.ilike(like), Listing.description.ilike(like), Listing.city.ilike(like)))
        return stmt

    @app.get("/api/listings")
    def listings(
        s: Session = S, game: str | None = None, marketplace: str | None = None, edition: str | None = None,
        min_value: float | None = None, min_ratio: float | None = None, q: str | None = None,
        found: str | None = Query(None, pattern="^(photo|text)$"), status: str = "active", include_unmatched: bool = False, review: bool = True,
        sort: str = Query("value", pattern="^(value|ratio|score|newest|price)$"),
        limit: int = Query(100, le=500), offset: int = 0,
    ):
        order = {
            "value": Listing.est_total_value.desc(), "ratio": Listing.value_ratio.desc().nulls_last(),
            "score": Listing.est_score.desc(), "newest": Listing.first_seen_at.desc(), "price": Listing.price_eur.asc().nulls_last(),
        }[sort]
        rows = list(s.scalars(_filtered(game, marketplace, edition, min_value, min_ratio, q, status, include_unmatched, review).order_by(order, Listing.id)))
        if edition:  # JSON column: filter in Python to stay DB-agnostic
            rows = [r for r in rows if edition in (r.editions or [])]
        if found:  # "photo": recognised only in the ad photos
            rows = [r for r in rows if ("photo_match" in (r.flags or [])) == (found == "photo")]
        total = len(rows)
        return {"total": total, "items": [listing_out(r) for r in rows[offset : offset + limit]]}

    @app.get("/api/listings/{listing_id:path}")
    def listing(listing_id: str, s: Session = S):
        row = s.get(Listing, listing_id)
        if not row:
            raise HTTPException(404)
        return listing_out(row)

    @app.post("/api/matches/{match_id}")
    def review_match(match_id: int, body: MatchReview, s: Session = S):
        m = s.get(Match, match_id)
        if not m or body.status not in {"confirmed", "rejected", "auto"}:
            raise HTTPException(400 if m else 404)
        m.status = body.status
        l = m.listing
        v = value_listing(l.matches, l.price_eur)
        l.est_total_value, l.est_score, l.value_ratio, l.match_count = v.total_value, v.score, v.ratio, v.counted
        return listing_out(l)

    @app.get("/api/map")
    def map_points(s: Session = S, game: str | None = None, marketplace: str | None = None, min_value: float | None = None):
        stmt = _filtered(game, marketplace, None, min_value, None, None, "active", False, True).where(Listing.lat.is_not(None))
        return [
            {"id": l.id, "lat": l.lat, "lon": l.lon, "precision": l.geo_precision, "title": l.title, "price_eur": l.price_eur,
             "value_eur": l.est_total_value, "ratio": l.value_ratio, "marketplace": l.marketplace, "url": l.url,
             "city": l.city, "region": l.region, "needs_review": l.needs_review}
            for l in s.scalars(stmt)
        ]

    # ---- deals (workflow) ----
    @app.get("/api/deals")
    def deals(s: Session = S, stage: str | None = None):
        stmt = select(Deal).order_by(Deal.updated_at.desc())
        if stage:
            stmt = stmt.where(Deal.stage == stage)
        return [deal_out(d) for d in s.scalars(stmt)]

    @app.post("/api/listings/{listing_id:path}/deal")
    def open_deal(listing_id: str, s: Session = S):
        l = s.get(Listing, listing_id)
        if not l:
            raise HTTPException(404)
        if l.deal:
            return deal_out(l.deal)
        d = Deal(listing_id=l.id, stage="new")
        s.add(d)
        s.flush()
        events.emit(s, events.DEAL_OPENED, l.id, deal_id=d.id, value=l.est_total_value, ratio=l.value_ratio, manual=True)
        s.refresh(l)
        return deal_out(d)

    @app.post("/api/deals/{deal_id}/transition")
    def deal_transition(deal_id: int, body: TransitionIn, s: Session = S):
        d = s.get(Deal, deal_id)
        if not d:
            raise HTTPException(404)
        try:
            transition(s, d, body.to, by=body.by, note=body.note, **body.fields)
        except (InvalidTransition, AttributeError) as e:
            raise HTTPException(409, str(e)) from e
        return deal_out(d)

    @app.post("/api/deals/{deal_id}/notes")
    def deal_note(deal_id: int, body: NoteIn, s: Session = S):
        d = s.get(Deal, deal_id)
        if not d:
            raise HTTPException(404)
        add_note(d, body.by, body.text)
        return deal_out(d)

    # ---- outbox for external agents ----
    @app.get("/api/events")
    def get_events(s: Session = S, agent: str | None = None, types: str | None = None, limit: int = Query(20, le=200)):
        tlist = types.split(",") if types else None
        if agent:  # claim mode
            return [event_out(e) for e in events.claim(s, agent, tlist, limit)]
        stmt = select(Event).order_by(Event.id.desc()).limit(limit)
        if tlist:
            stmt = stmt.where(Event.type.in_(tlist))
        return [event_out(e) for e in s.scalars(stmt)]

    @app.post("/api/events/{event_id}/ack")
    def ack_event(event_id: int, body: AckIn, s: Session = S):
        ev = events.ack(s, event_id, body.error)
        if not ev:
            raise HTTPException(404)
        return event_out(ev)

    # ---- scans ----
    @app.get("/api/scans")
    def scans(s: Session = S, limit: int = 20):
        return [{"id": r.id, "started_at": r.started_at, "finished_at": r.finished_at, "stats": r.stats, "errors": r.errors}
                for r in s.scalars(select(ScanRun).order_by(ScanRun.id.desc()).limit(limit))]

    @app.post("/api/scan", status_code=202)
    def trigger_scan(body: ScanIn):
        if not scan_lock.acquire(blocking=False):
            raise HTTPException(409, "scan already running")

        def work():
            from ..pipeline import Scanner

            try:
                Scanner(settings, db).run(body.games, body.marketplaces, body.max_queries)
            finally:
                scan_lock.release()

        threading.Thread(target=work, daemon=True).start()
        return {"status": "started"}

    # ---- dashboard ----
    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB / "index.html")

    return app
