"""Deal state machine shared by all downstream agents."""
from __future__ import annotations

from sqlalchemy.orm import Session

from .. import events
from ..config import Settings
from ..models import Deal, Listing, utcnow

STAGES = ["new", "shortlisted", "contacted", "negotiating", "agreed", "paid", "shipped", "received", "rejected", "cancelled"]
FINAL = {"received", "rejected", "cancelled"}

TRANSITIONS: dict[str, set[str]] = {
    "new": {"shortlisted", "contacted", "rejected"},
    "shortlisted": {"contacted", "rejected"},
    "contacted": {"negotiating", "agreed", "rejected"},
    "negotiating": {"agreed", "rejected"},
    "agreed": {"paid", "cancelled"},
    "paid": {"shipped", "cancelled"},
    "shipped": {"received", "cancelled"},
}


class InvalidTransition(ValueError):
    pass


def can_transition(src: str, dst: str) -> bool:
    return dst in TRANSITIONS.get(src, set())


def transition(session: Session, deal: Deal, to_stage: str, by: str, note: str | None = None, **fields) -> Deal:
    if to_stage not in STAGES:
        raise InvalidTransition(f"unknown stage {to_stage!r}")
    if not can_transition(deal.stage, to_stage):
        raise InvalidTransition(f"{deal.stage} -> {to_stage} not allowed")
    src = deal.stage
    deal.stage = to_stage
    for k, v in fields.items():
        if not hasattr(deal, k):
            raise AttributeError(k)
        setattr(deal, k, v)
    add_note(deal, by, note or f"{src} -> {to_stage}")
    events.emit(session, events.DEAL_STAGE_CHANGED, deal.listing_id, deal_id=deal.id, src=src, dst=to_stage, by=by)
    return deal


def add_note(deal: Deal, by: str, text: str) -> None:
    deal.notes = [*(deal.notes or []), {"at": utcnow().isoformat(), "by": by, "text": text}]


def maybe_open_deal(session: Session, settings: Settings, listing: Listing) -> Deal | None:
    if listing.deal is not None or listing.status != "active":
        return None
    cfg = settings.deals
    gfilters = settings.games[listing.game].filters if listing.game in settings.games else None
    if gfilters and gfilters.max_listing_price_eur and (listing.price_eur or 0) > gfilters.max_listing_price_eur:
        return None
    if listing.est_total_value < cfg.auto_open_min_value_eur:
        return None
    if listing.value_ratio is not None and listing.value_ratio < cfg.auto_open_min_ratio:
        return None
    deal = Deal(listing_id=listing.id, stage="new")
    session.add(deal)
    session.flush()
    events.emit(session, events.DEAL_OPENED, listing.id, deal_id=deal.id, value=listing.est_total_value, ratio=listing.value_ratio)
    return deal


def maybe_close_deal(session: Session, listing: Listing) -> Deal | None:
    """Reject a deal nobody has acted on yet once its ad no longer has Alpha/Beta cards
    (e.g. after a re-check vetoed the match). Deals already in contact are left to humans."""
    deal = listing.deal
    if deal is None or listing.match_count or deal.stage not in ("new", "shortlisted"):
        return None
    return transition(session, deal, "rejected", by="system:recheck",
                      note=f"auto-rejected: no Alpha/Beta cards after re-check ({', '.join(listing.flags) or 'no matches'})")
