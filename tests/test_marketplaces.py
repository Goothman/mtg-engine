import json
from pathlib import Path

import httpx
import pytest
import respx

from mtg_engine.marketplaces import get_marketplace
from mtg_engine.marketplaces.milanuncios import BlockedError, parse_search_page
from mtg_engine.marketplaces.wallapop import API_URL, extract_items, parse_item

FX = Path(__file__).parent / "fixtures"


def test_wallapop_parse():
    items, nxt = extract_items(json.loads((FX / "wallapop_search.json").read_text()))
    assert nxt == "TOKEN2" and len(items) == 2
    l = parse_item(items[0])
    assert l.key == "wallapop:abc123"
    assert l.price_eur == 300.0 and (l.lat, l.lon) == (40.41, -3.70)
    assert l.url.endswith("/item/lightning-bolt-alpha-magic-123")
    assert l.images == ["https://cdn.example/b.jpg"] and l.published_at.year == 2025
    assert parse_item(items[1]).lat is None


def test_wallapop_legacy_shape():
    items, nxt = extract_items({"search_objects": [{"id": 1, "title": "x"}]})
    assert len(items) == 1 and nxt is None


@respx.mock
def test_wallapop_paginates(settings):
    payload = json.loads((FX / "wallapop_search.json").read_text())
    last = {"data": {"section": {"payload": {"items": []}}}, "meta": {}}
    route = respx.get(API_URL).mock(side_effect=[httpx.Response(200, json=payload), httpx.Response(200, json=last)])
    settings.marketplaces["wallapop"].delay_seconds = 0
    wp = get_marketplace("wallapop", settings)
    got = list(wp.search("magic alpha", settings.region.search_points[0]))
    assert len(got) == 2 and route.call_count == 2
    assert route.calls[0].request.url.params["distance"] == str(settings.region.search_radius_km * 1000)
    assert route.calls[1].request.url.params["next_page"] == "TOKEN2"


def test_milanuncios_initial_props():
    ads = parse_search_page((FX / "milanuncios_initial_props.html").read_text())
    assert [a.external_id for a in ads] == ["512345678", "598765432"]
    a = ads[0]
    assert a.price_eur == 15000 and a.city == "Getafe" and a.region == "Madrid"
    assert a.url == "https://www.milanuncios.com/cartas/black-lotus-beta-512345678.htm"
    assert ads[1].price_eur == 120


def test_milanuncios_html_fallback():
    ads = parse_search_page((FX / "milanuncios_cards.html").read_text())
    assert len(ads) == 1
    a = ads[0]
    assert a.external_id == "511112222" and a.price_eur == 1250.5
    assert a.city == "Alcalá de Henares" and a.region == "Madrid"


def test_milanuncios_blocked():
    with pytest.raises(BlockedError):
        parse_search_page("<html><title>Pardon Our Interruption</title><script src='/_Incapsula_Resource'></script></html>")


def test_proxy_env_overrides(monkeypatch):
    from mtg_engine.config import load_settings

    monkeypatch.setenv("MTG_ENGINE_PROXY_WALLAPOP", "http://u:p@es.example:8080")
    s = load_settings(overrides={"database_url": "sqlite:///:memory:"})
    assert s.marketplaces["wallapop"].proxy == "http://u:p@es.example:8080"
    assert s.marketplaces["milanuncios"].proxy is None


def test_playwright_proxy_parsing():
    from mtg_engine.marketplaces.milanuncios import _playwright_proxy

    assert _playwright_proxy("http://u:p@es.example:8080") == {"server": "http://es.example:8080", "username": "u", "password": "p"}
