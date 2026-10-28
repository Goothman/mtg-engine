from types import SimpleNamespace as NS

from mtg_engine.valuation import value_listing


def m(q, c, v, status="auto"):
    return NS(quantity=q, confidence=c, unit_value_eur=v, status=status)


def test_value_counts_confident_matches_only():
    v = value_listing([m(4, 0.9, 100), m(1, 0.2, 1000), m(1, 0.3, 50, "confirmed"), m(1, 0.9, 500, "rejected")], 200)
    assert v.total_value == 450 and v.counted == 2
    assert v.ratio == 2.25
    assert v.score == 0.9 * 400 + 50


def test_no_price_no_ratio():
    assert value_listing([m(1, 0.9, 10)], None).ratio is None
