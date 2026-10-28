from mtg_engine.games.mtg import scryfall_to_card
from mtg_engine.pricing.cardmarket import parse_price_guide


def test_price_guide_parse():
    g = parse_price_guide({"priceGuides": [
        {"idProduct": 7, "trend": None, "avg30": 12.5, "avg": 10, "low": 8},
        # real Alpha Island row: trend/avg7 spiked by a single sale
        {"idProduct": 8, "avg": 46.23, "low": 31.99, "trend": 1403.07, "avg1": 39, "avg7": 1307.8, "avg30": 228.42},
    ]})
    assert g[7].best() == 10 and g[7].low == 8
    assert g[8].best() == 137.32


def test_scryfall_eur_then_usd_fallback():
    base = {"id": "x", "set": "lea", "set_name": "Limited Edition Alpha", "name": "Fog", "cardmarket_id": 5}
    assert scryfall_to_card({**base, "prices": {"eur": "20.5"}}, 0.9).price_eur == 20.5
    c = scryfall_to_card({**base, "prices": {"usd": "100"}}, 0.9)
    assert c.price_eur == 90 and c.price_source == "scryfall_usd_fx"
