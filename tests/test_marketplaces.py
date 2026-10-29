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
    assert route.calls[0].request.url.params["distance_in_km"] == str(settings.region.search_radius_km)
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


def test_wallapop_real_capture():
    """Real api.wallapop.com response captured 2026-10-28 (query "magic alpha", Madrid)."""
    items, nxt = extract_items(json.loads((FX / "wallapop_search_real.json").read_text()))
    assert len(items) == 35 and nxt
    first = parse_item(items[0])
    assert first.title == "Magic Alpha Island 7.5 NM" and first.price_eur == 70.0
    assert first.region == "Madrid" and first.lat and first.images
    assert all(parse_item(i).external_id for i in items)


def mock_apify(items, status="SUCCEEDED"):
    """Mock Apify's async flow: start run -> poll -> paged dataset. Returns the start-run route."""
    start = respx.post("https://api.apify.com/v2/acts/zen-studio~milanuncios-scraper/runs").mock(
        return_value=httpx.Response(201, json={"data": {"id": "r1", "status": "RUNNING", "defaultDatasetId": "d1"}}))
    respx.get("https://api.apify.com/v2/actor-runs/r1").mock(
        return_value=httpx.Response(200, json={"data": {"id": "r1", "status": status, "defaultDatasetId": "d1"}}))

    def page(request):
        off, lim = int(request.url.params["offset"]), int(request.url.params["limit"])
        return httpx.Response(200, json=items[off: off + lim])

    respx.get("https://api.apify.com/v2/datasets/d1/items").mock(side_effect=page)
    return start


@respx.mock
def test_milanuncios_apify_fetcher(settings, monkeypatch):
    """Item format of zen-studio/milanuncios-scraper is assumed; the parser accepts common variants."""
    from mtg_engine.marketplaces.milanuncios import Milanuncios

    cfg = settings.marketplaces["milanuncios"].model_copy(update={"fetcher": "apify", "max_results": 5})
    route = mock_apify([
            {"id": "512345678", "title": "Black Lotus beta", "description": "original", "price": "1.200 €",
             "url": "/cartas-coleccionables/black-lotus-512345678.htm", "location": "Getafe (Madrid)",
             "images": [{"url": "https://images.milanuncios.com/a.jpg"}], "seller": {"name": "Ana", "id": 77}},
            {"url": "https://www.milanuncios.com/x/lote-magic-598765432.htm", "title": "Lote magic",
             "location": {"city": "Sevilla", "province": "Sevilla"}, "photos": ["https://images.milanuncios.com/b.jpg"]},
            {"title": "no id at all"},
        ])
    monkeypatch.setenv("APIFY_TOKEN", "test-token")
    m = Milanuncios(settings, cfg)
    ads = list(m.search("magic beta"))
    m.close()
    assert route.calls.last.request.headers["Authorization"] == "Bearer test-token"
    assert json.loads(route.calls.last.request.content) == {"text": "magic beta", "maxResults": 5, "sort": "relevance", "isShippable": False}
    a, b = ads
    assert (a.external_id, a.price_eur, a.city, a.region, a.seller_name, a.images) == (
        "512345678", 1200.0, "Getafe", "Madrid", "Ana", ["https://images.milanuncios.com/a.jpg"])  # not an api/ URL
    assert a.url == "https://www.milanuncios.com/cartas-coleccionables/black-lotus-512345678.htm"
    assert (b.external_id, b.city, b.images) == ("598765432", "Sevilla", ["https://images.milanuncios.com/b.jpg"])


def test_milanuncios_apify_needs_token(settings, monkeypatch):
    from mtg_engine.marketplaces.milanuncios import BlockedError, Milanuncios

    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    m = Milanuncios(settings, settings.marketplaces["milanuncios"].model_copy(update={"fetcher": "apify"}))
    with pytest.raises(BlockedError):
        list(m.search("magic"))


@respx.mock
def test_milanuncios_apify_run_budget(settings, monkeypatch):
    from mtg_engine.marketplaces.milanuncios import Milanuncios

    cfg = settings.marketplaces["milanuncios"].model_copy(
        update={"fetcher": "apify", "max_results": 100, "max_results_per_run": 250})
    route = mock_apify([])
    monkeypatch.setenv("APIFY_TOKEN", "t")
    m = Milanuncios(settings, cfg)
    for q in ("a", "b", "c", "d"):
        list(m.search(q))
    m.close()
    assert [json.loads(c.request.content)["maxResults"] for c in route.calls] == [100, 100, 50]


@respx.mock
def test_milanuncios_apify_real_items(settings, monkeypatch):
    """Items as returned live by zen-studio/milanuncios-scraper (seller anonymised)."""
    from mtg_engine.marketplaces.milanuncios import Milanuncios

    items = json.loads((Path(__file__).parent / "fixtures" / "milanuncios_apify_real.json").read_text())
    mock_apify(items)
    monkeypatch.setenv("APIFY_TOKEN", "t")
    m = Milanuncios(settings, settings.marketplaces["milanuncios"].model_copy(update={"fetcher": "apify"}))
    ads = list(m.search("magic"))
    a, b = ads  # the DVD (category reproductores-de-dvd) is dropped
    assert (a.external_id, a.price_eur, a.city, a.region, a.postal_code) == ("621016983", 55.0, "Málaga", "Málaga", "29006")
    assert (a.lat, a.lon, a.seller_id, a.seller_name) == (36.7185, -4.4355, "1000", "Vendedor 0")
    assert a.url == "https://www.milanuncios.com/cartas-de-coleccion/magic-the-gathering-621016983.htm"
    assert a.images[0].startswith("https://images.milanuncios.com/") and a.images[0].endswith("?rule=hw545")
    assert a.published_at.year == 2026
    assert b.price_eur == 25.0 and b.city == "Las Palmas de Gran Canaria"
    assert m.seller_profile("1000")["name"] == "Vendedor 0" and m.seller_profile("1000")["type"] == "Private"
    m.close()


@respx.mock
def test_milanuncios_apify_pages_and_partial_runs(settings, monkeypatch):
    """Large crawls: the dataset is read in pages; a run that timed out still yields what it collected."""
    import mtg_engine.marketplaces.milanuncios as mm

    monkeypatch.setattr(mm, "APIFY_PAGE", 2)
    items = [{"id": str(600000000 + i), "title": f"Cartas magic {i}", "url": f"/x/a-{600000000 + i}.htm"} for i in range(5)]
    mock_apify(items, status="TIMED-OUT")
    monkeypatch.setenv("APIFY_TOKEN", "t")
    monkeypatch.setenv("MTG_ENGINE_MAX_RESULTS_MILANUNCIOS", "10000")
    from mtg_engine.config import load_settings

    st = load_settings(overrides={"database_url": "sqlite:///:memory:"})
    cfg = st.marketplaces["milanuncios"]
    assert (cfg.max_results, cfg.max_results_per_run) == (10000, None)
    m = mm.Milanuncios(st, cfg.model_copy(update={"fetcher": "apify", "category_slugs": []}))
    assert len(list(m.search("cartas magic"))) == 5
    m.close()
