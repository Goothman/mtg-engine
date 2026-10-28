"""Starter agents. They do safe, deterministic work today and mark where LLM-backed
or platform-integrated behaviour plugs in later."""
from __future__ import annotations

from sqlalchemy import select

from .. import events
from ..models import Deal, Event, Listing, utcnow
from .base import Agent, AgentContext
from .workflow import add_note, transition


def _deal(ctx: AgentContext, event: Event) -> Deal | None:
    deal_id = event.payload.get("deal_id")
    if deal_id:
        return ctx.session.get(Deal, deal_id)
    return ctx.session.scalar(select(Deal).where(Deal.listing_id == event.subject_id))


class OutreachAgent(Agent):
    """On a new deal: draft a first-contact message in Spanish (asks for authenticity
    photos) and shortlist the deal. A human approves/sends the draft."""

    name = "outreach"
    subscribes = (events.DEAL_OPENED,)

    def handle(self, event: Event, ctx: AgentContext) -> None:
        deal = _deal(ctx, event)
        if deal is None or deal.stage != "new":
            return
        listing: Listing = deal.listing
        cards = ", ".join(sorted({m.card.name for m in listing.matches if m.confidence >= 0.5})) or "las cartas"
        text = (
            f"Hola, me interesa tu anuncio \"{listing.title}\". "
            f"¿Podrías enviarme fotos de las esquinas, el dorso y el borde de {cards}? "
            "¿Siguen disponibles? Podría pagar por la plataforma con envío. ¡Gracias!"
        )
        deal.drafts = [*(deal.drafts or []), {"at": utcnow().isoformat(), "by": self.name, "channel": ctx.messaging.name, "text": text, "status": "pending_approval"}]
        events.emit(ctx.session, events.DEAL_DRAFT_CREATED, listing.id, deal_id=deal.id, kind="first_contact")
        transition(ctx.session, deal, "shortlisted", by=self.name, note="first-contact draft created")


class NegotiationAgent(Agent):
    """Suggests an opening offer once a deal is contacted. Policy: never above asking;
    aim for a fraction of estimated value depending on how good the deal already is."""

    name = "negotiation"
    subscribes = (events.DEAL_STAGE_CHANGED,)

    def handle(self, event: Event, ctx: AgentContext) -> None:
        if event.payload.get("dst") != "contacted":
            return
        deal = _deal(ctx, event)
        if deal is None:
            return
        listing = deal.listing
        asking = listing.price_eur or 0
        value = listing.est_total_value
        if not asking or not value:
            return
        target = min(asking, round(value * (0.55 if (listing.value_ratio or 0) < 2 else 0.7), 0))
        opening = round(min(asking * 0.85, target), 0)
        deal.offer_price_eur = opening
        add_note(deal, self.name, f"suggested opening offer {opening}€ (asking {asking}€, value ~{value}€, walk-away {target}€)")


class LogisticsAgent(Agent):
    """Placeholder: once paid, create a shipment via a ShippingChannel (Wallapop Envíos,
    Correos, SEUR...) and store tracking on deal.shipping."""

    name = "logistics"
    subscribes = (events.DEAL_STAGE_CHANGED,)

    def handle(self, event: Event, ctx: AgentContext) -> None:
        if event.payload.get("dst") != "paid":
            return
        deal = _deal(ctx, event)
        if deal is not None:
            add_note(deal, self.name, "awaiting shipping channel integration")


DEFAULT_AGENTS = [OutreachAgent(), NegotiationAgent(), LogisticsAgent()]
