"""Demo data for previewing the dashboard without network access.

Everything here is fictional: ads, sellers and URLs are invented, and card prices are
rough placeholders (price_source="demo"), NOT market data. Demo rows are flagged
(`is_demo`, `extra.demo`) and removed by `mtg-engine clear-demo`. A real
`sync-catalog` overwrites demo card prices with live ones.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .agents.workflow import maybe_open_deal
from .config import Settings
from .events import LISTING_DISCOVERED, LISTING_MATCHED, emit
from .geo import resolve
from .models import CardPrinting, Deal, Event, Listing, utcnow
from .pipeline import analyse, build_matcher
from .text import normalize

# (name, rarity, type, alpha_price, beta_price): placeholder values for UI preview only
DEMO_CARDS = [
    ("Black Lotus", "rare", "Artifact", 30000, 18000),
    ("Mox Sapphire", "rare", "Artifact", 9000, 6500),
    ("Time Walk", "rare", "Sorcery", 7000, 4500),
    ("Ancestral Recall", "rare", "Instant", 9500, 6000),
    ("Lightning Bolt", "common", "Instant", 450, 220),
    ("Dark Ritual", "common", "Instant", 180, 90),
    ("Shivan Dragon", "rare", "Creature — Dragon", 1200, 700),
    ("Serra Angel", "uncommon", "Creature — Angel", 300, 160),
    ("Llanowar Elves", "common", "Creature — Elf Druid", 110, 55),
    ("Counterspell", "uncommon", "Instant", 350, 180),
    ("Island", "land", "Basic Land — Island", 25, 12),
    ("Sol Ring", "uncommon", "Artifact", 2600, 1500),
]

DEMO_ADS = [
    ("wallapop", "Lote cartas Magic Alpha 1993 - Lightning Bolt x4, Dark Ritual x2", "Cartas de mi infancia, edición Alpha esquinas redondeadas. 4x Lightning Bolt, 2x Dark Ritual, Llanowar Elves. Envío disponible.", 600, None, None, 40.4381, -3.6796, "Madrid", "Madrid"),
    ("wallapop", "Shivan Dragon beta magic", "Shivan Dragon edición Beta, buen estado, ligera marca en el dorso.", 450, None, None, 41.4036, 2.1744, "Barcelona", "Barcelona"),
    ("milanuncios", "Magic the Gathering colección antigua (beta) Serra Angel, Counterspell", "Vendo Serra Angel y 2x Counterspell beta. También islas.", 280, "Valencia", "Valencia", None, None, None, None),
    ("wallapop", "Black Lotus alpha??", "Encontrada en el trastero, no sé si es original. Black Lotus alpha.", 2500, None, None, 37.3828, -5.9732, "Sevilla", "Sevilla"),
    ("milanuncios", "Sol Ring Beta Magic", "Sol Ring edición limitada beta. Sin jugar.", 900, "Bilbao", "Bizkaia", None, None, None, None),
    ("wallapop", "Cartas magic antiguas alpha", "Lote de unas 60 cartas alpha, ver fotos. No sé nombres.", 150, None, None, 43.3623, -8.4115, "A Coruña", "A Coruña"),
    ("milanuncios", "Lightning bolt magic (proxy)", "Proxy de Lightning Bolt alpha, para jugar casual.", 3, "Málaga", "Málaga", None, None, None, None),
    ("wallapop", "Mox Sapphire + Time Walk Alpha", "Dos cartas power nine edición alpha, buen estado, solo entrega en mano en Zaragoza.", 12000, None, None, 41.6561, -0.8773, "Zaragoza", "Zaragoza"),
]


def seed_demo(session: Session, settings: Settings) -> int:
    clear_demo(session)
    targets = settings.games["mtg"].targets
    codes = [t.set_code for t in targets][:2]
    for name, rarity, type_line, *prices in DEMO_CARDS:
        for code, price in zip(codes, prices):
            cid = f"demo-{code}-{normalize(name).replace(' ', '-')}"
            if session.get(CardPrinting, cid) or session.scalar(
                select(CardPrinting).where(CardPrinting.name == name, CardPrinting.set_code == code)
            ):
                continue
            session.add(CardPrinting(
                id=cid, game="mtg", set_code=code, set_name=next(t.label for t in targets if t.set_code == code),
                name=name, name_norm=normalize(name), rarity=rarity, type_line=type_line, is_target=True,
                price_eur=float(price), price_source="demo", extra={"demo": True},
            ))
    session.flush()
    matcher = build_matcher(session, settings, "mtg")
    now = utcnow()
    for i, (market, title, desc, price, city, region, lat, lon, wcity, wregion) in enumerate(DEMO_ADS):
        key = f"{market}:demo-{i}"
        glat, glon, precision, province = resolve("ES", lat, lon, city or wcity, region or wregion, key)
        listing = Listing(
            id=key, marketplace=market, external_id=f"demo-{i}", game="mtg", url="#demo", title=title,
            description=desc, price_eur=float(price), city=city or wcity, region=province, lat=glat, lon=glon,
            geo_precision=precision, images=[], is_demo=True, first_seen_at=now - timedelta(hours=3 * i),
            last_seen_at=now, seller_name="demo seller",
        )
        session.add(listing)
        session.flush()
        analyse(session, settings, listing, matcher)
        emit(session, LISTING_DISCOVERED, key, marketplace=market, demo=True)
        if listing.match_count:
            emit(session, LISTING_MATCHED, key, value=listing.est_total_value, ratio=listing.value_ratio, demo=True)
            maybe_open_deal(session, settings, listing)
    return len(DEMO_ADS)


def clear_demo(session: Session) -> int:
    ids = list(session.scalars(select(Listing.id).where(Listing.is_demo.is_(True))))
    if ids:
        session.execute(delete(Deal).where(Deal.listing_id.in_(ids)))
        session.execute(delete(Event).where(Event.subject_id.in_(ids)))
        for l in session.scalars(select(Listing).where(Listing.id.in_(ids))):
            session.delete(l)
        session.flush()
    session.execute(delete(CardPrinting).where(CardPrinting.price_source == "demo"))
    return len(ids)
