"""Agent framework.

An agent subscribes to event types and reacts by updating deals (through the
workflow state machine), creating message drafts, or calling channels. Agents run
in-process (`mtg-engine agents run`) or as external services over the HTTP API
(GET /api/events?agent=..., POST /api/events/{id}/ack, POST /api/deals/{id}/transition).
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session

from .. import events
from ..config import Settings
from ..models import Event

log = logging.getLogger(__name__)


# ---- channels: keep agents independent of concrete platforms -------------------------

class MessagingChannel(Protocol):
    name: str

    def send(self, listing_id: str, seller_id: str | None, text: str) -> str: ...


class PaymentChannel(Protocol):
    def request_payment(self, deal_id: int, amount_eur: float) -> str: ...


class ShippingChannel(Protocol):
    def create_shipment(self, deal_id: int, address: dict) -> dict: ...


class DraftOnlyMessaging:
    """Default channel: messages are stored as drafts for a human to approve/send.
    Wallapop and Milanuncios don't offer a public messaging API."""

    name = "draft"

    def send(self, listing_id: str, seller_id: str | None, text: str) -> str:
        raise NotImplementedError("draft-only channel: approve and send the draft manually")


# ---- agent base ----------------------------------------------------------------------

@dataclass
class AgentContext:
    session: Session
    settings: Settings
    messaging: MessagingChannel


class Agent(ABC):
    name: str
    subscribes: tuple[str, ...] = ()

    @abstractmethod
    def handle(self, event: Event, ctx: AgentContext) -> None:
        """Process one event. Raise to have it retried."""


class AgentRunner:
    def __init__(self, agents: list[Agent]):
        self.agents = agents

    def run_once(self, ctx: AgentContext) -> int:
        processed = 0
        for agent in self.agents:
            for ev in events.claim(ctx.session, agent.name, list(agent.subscribes)):
                try:
                    with ctx.session.begin_nested():
                        agent.handle(ev, ctx)
                    events.ack(ctx.session, ev.id)
                    processed += 1
                except Exception as e:  # keep going; event is released for retry
                    log.exception("agent %s failed on event %s", agent.name, ev.id)
                    events.ack(ctx.session, ev.id, error=f"{type(e).__name__}: {e}")
        return processed
