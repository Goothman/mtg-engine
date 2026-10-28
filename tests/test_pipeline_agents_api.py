from fastapi.testclient import TestClient
from sqlalchemy import select

from mtg_engine.agents import DEFAULT_AGENTS, AgentContext, AgentRunner, DraftOnlyMessaging, InvalidTransition, transition
from mtg_engine.api import create_app
from mtg_engine.config import MarketplaceConfig
from mtg_engine.marketplaces import REGISTRY, Marketplace, RawListing
from mtg_engine.models import Deal, Event, Listing
from mtg_engine.pipeline import Scanner

ADS = [
    RawListing("fake", "1", "https://x/1", "Lightning Bolt x4 alpha", "", 300.0, city="Madrid"),
    RawListing("fake", "2", "https://x/2", "Black Lotus beta", "original", 9000.0, lat=41.0, lon=2.0),
    RawListing("fake", "3", "https://x/3", "Cartas pokemon", "", 5.0),
    RawListing("fake", "4", "https://x/4", "Lote magic alpha", "ver fotos", 50.0, region="Sevilla"),
]


class FakeMarket(Marketplace):
    name = "fake"

    def search(self, query, point=None):
        yield from ADS


def run_scan(settings, db):
    REGISTRY["fake"] = FakeMarket
    settings.marketplaces = {"fake": MarketplaceConfig(use_search_points=False, delay_seconds=0)}
    settings.games["pokemon"].enabled = False
    return Scanner(settings, db).run(max_queries=1)


def test_scan_values_and_events(settings, db):
    stats = run_scan(settings, db)
    assert stats["fetched"] == 4 and stats["new"] == 4 and stats["matched"] == 2 and stats["errors"] == 0
    with db.session() as s:
        bolt = s.get(Listing, "fake:1")
        assert bolt.est_total_value == 1600 and bolt.value_ratio == 5.333 and bolt.geo_precision == "city"
        lotus = s.get(Listing, "fake:2")
        assert lotus.est_total_value == 18000 and lotus.geo_precision == "exact"
        assert s.get(Listing, "fake:4").needs_review
        assert s.get(Listing, "fake:3").match_count == 0
        types = [e.type for e in s.scalars(select(Event))]
        assert types.count("listing.discovered") == 4 and types.count("deal.opened") == 2
    # second scan: nothing changed -> no new events
    stats = run_scan(settings, db)
    assert stats["new"] == 0 and stats["updated"] == 0


def test_agents_and_state_machine(settings, db):
    run_scan(settings, db)
    with db.session() as s:
        n = AgentRunner(DEFAULT_AGENTS).run_once(AgentContext(s, settings, DraftOnlyMessaging()))
        assert n >= 2
        deal = s.scalar(select(Deal).where(Deal.listing_id == "fake:1"))
        assert deal.stage == "shortlisted" and "Lightning Bolt" in deal.drafts[0]["text"]
        transition(s, deal, "contacted", by="human")
        try:
            transition(s, deal, "shipped", by="human")
            raise AssertionError("should not allow contacted -> shipped")
        except InvalidTransition:
            pass
    with db.session() as s:
        AgentRunner(DEFAULT_AGENTS).run_once(AgentContext(s, settings, DraftOnlyMessaging()))
        deal = s.scalar(select(Deal).where(Deal.listing_id == "fake:1"))
        assert deal.offer_price_eur and deal.offer_price_eur <= 300 * 0.85


def test_api(settings, db):
    run_scan(settings, db)
    c = TestClient(create_app(settings, db))
    assert c.get("/").status_code == 200
    st = c.get("/api/stats").json()
    assert st["listings_matched"] == 2 and st["total_value_eur"] == 19600
    items = c.get("/api/listings").json()["items"]
    assert [i["id"] for i in items] == ["fake:2", "fake:1", "fake:4"]  # ranked by value, review last
    assert [i["id"] for i in c.get("/api/listings?sort=ratio&review=false").json()["items"]] == ["fake:1", "fake:2"]
    assert c.get("/api/listings?edition=leb").json()["total"] == 1
    assert len(c.get("/api/map").json()) == 3  # fake:3 unmatched, not shown
    # reject the bolt match -> value drops
    mid = items[1]["matches"][0]["id"]
    assert c.post(f"/api/matches/{mid}", json={"status": "rejected"}).json()["est_total_value"] == 0
    # outbox claim/ack over HTTP
    evs = c.get("/api/events?agent=ext&types=deal.opened").json()
    assert len(evs) == 2
    assert c.get("/api/events?agent=ext2&types=deal.opened").json() == []  # already claimed
    assert c.post(f"/api/events/{evs[0]['id']}/ack", json={}).status_code == 200
    # deal transitions via HTTP
    deal = c.get("/api/deals").json()[0]
    assert c.post(f"/api/deals/{deal['id']}/transition", json={"to": "shipped"}).status_code == 409
    r = c.post(f"/api/deals/{deal['id']}/transition", json={"to": "contacted", "by": "agent:x", "fields": {"offer_price_eur": 100}})
    assert r.status_code == 200 and r.json()["stage"] == "contacted" and r.json()["offer_price_eur"] == 100
    # manual deal on review listing
    assert c.post("/api/listings/fake:4/deal").json()["stage"] == "new"
