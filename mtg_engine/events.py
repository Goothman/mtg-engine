"""Transactional outbox. Events are written in the same DB transaction as the change
that caused them; agents claim, process and ack them (at-least-once delivery)."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .models import Event, utcnow

LISTING_DISCOVERED = "listing.discovered"
LISTING_MATCHED = "listing.matched"
LISTING_UPDATED = "listing.updated"
LISTING_GONE = "listing.gone"
DEAL_OPENED = "deal.opened"
DEAL_STAGE_CHANGED = "deal.stage_changed"
DEAL_DRAFT_CREATED = "deal.draft_created"

CLAIM_TIMEOUT = timedelta(minutes=10)
MAX_ATTEMPTS = 5


def emit(session: Session, type: str, subject_id: str | None = None, **payload: Any) -> Event:
    ev = Event(type=type, subject_id=subject_id, payload=payload)
    session.add(ev)
    return ev


def claim(session: Session, agent: str, types: list[str] | None = None, limit: int = 20) -> list[Event]:
    """Claim unprocessed events (or ones whose claim timed out) for `agent`."""
    now = utcnow()
    q = (
        select(Event)
        .where(Event.processed_at.is_(None), Event.attempts < MAX_ATTEMPTS)
        .where(or_(Event.claimed_at.is_(None), Event.claimed_at < now - CLAIM_TIMEOUT))
        .order_by(Event.id)
        .limit(limit)
    )
    if types:
        q = q.where(Event.type.in_(types))
    events = list(session.scalars(q))
    for ev in events:
        ev.claimed_by, ev.claimed_at = agent, now
        ev.attempts += 1
    session.flush()
    return events


def ack(session: Session, event_id: int, error: str | None = None) -> Event | None:
    ev = session.get(Event, event_id)
    if ev is None:
        return None
    if error:
        ev.error = error
        ev.claimed_at = None  # release for retry
    else:
        ev.processed_at = utcnow()
        ev.error = None
    return ev
