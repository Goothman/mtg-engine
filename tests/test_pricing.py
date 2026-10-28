from mtg_engine.games.mtg import scryfall_to_card
from mtg_engine.pricing.cardmarket import parse_price_guide


def test_price_guide_parse():
    g = parse_price_guide({"priceGuides": [{"idProduct": 7, "trend": None, "avg30": 12.5, "avg": 10, "low": 8}]})
    assert g[7].best() == 12.5 and g[7].low == 8


def test_scryfall_eur_then_usd_fallback():
    base = {"id": "x", "set": "lea", "set_name": "Limited Edition Alpha", "name": "Fog", "cardmarket_id": 5}
    assert scryfall_to_card({**base, "prices": {"eur": "20.5"}}, 0.9).price_eur == 20.5
    c = scryfall_to_card({**base, "prices": {"usd": "100"}}, 0.9)
    assert c.price_eur == 90 and c.price_source == "scryfall_usd_fx"
