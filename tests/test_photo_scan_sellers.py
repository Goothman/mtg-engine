"""Photo queries (vision on generic ads), recognition cache, seller profiles, column migration."""
import httpx
import respx
from sqlalchemy import create_engine, text

from mtg_engine.config import MarketplaceConfig, load_settings
from mtg_engine.db import Database
from mtg_engine.marketplaces import REGISTRY, Marketplace, RawListing
from mtg_engine.marketplaces.wallapop import Wallapop
from mtg_engine.models import ImageScan, Listing
from mtg_engine.pipeline import Scanner
from mtg_engine.vision.recognizer import Recognition

EDITIONS = ["lea", "leb", "2ed", "3ed", "ced", "other"]


def probs(edition, p):
    rest = (1 - p) / (len(EDITIONS) - 1)
    return {e: (p if e == edition else rest) for e in EDITIONS}


class FakeRecognizer:
    fingerprint = "test-model"

    def __init__(self, by_image):
        self.by_image, self.calls = by_image, 0

    def recognize_image(self, image_bytes):
        self.calls += 1
        return self.by_image.get(image_bytes, [])


PHOTO_ADS = [
    # generic lot, the photo shows an Alpha Black Lotus
    RawListing("fake", "p1", "https://x/p1", "Lote cartas magic", "muchas cartas", 40.0, seller_id="u1",
               images=["https://img.test/lotus.jpg"]),
    # generic lot, weak photo match -> ignored
    RawListing("fake", "p2", "https://x/p2", "Cartas magic", "", 10.0, seller_id="u2", images=["https://img.test/weak.jpg"]),
    # generic lot, confident but a non-target edition -> ignored, no red flag
    RawListing("fake", "p3", "https://x/p3", "Cartas magic antiguas", "", 20.0, seller_id="u1",
               images=["https://img.test/unlimited.jpg"]),
]


class PhotoMarket(Marketplace):
    name = "fake"
    profile_calls = 0

    def search(self, query, point=None):
        yield from PHOTO_ADS

    def seller_profile(self, seller_id):
        PhotoMarket.profile_calls += 1
        return {"name": f"Seller {seller_id}", "rating": 4.5}


def photo_scan(settings, db, recognizer):
    REGISTRY["fake"] = PhotoMarket
    settings.marketplaces = {"fake": MarketplaceConfig(use_search_points=False, delay_seconds=0)}
    settings.games["pokemon"].enabled = False
    settings.games["mtg"].photo_queries = ["cartas magic"]
    return Scanner(settings, db, recognizer).run(photo_queries=True)


@respx.mock
def test_photo_query_finds_cards_only_in_photos(settings, db):
    for name in ("lotus", "weak", "unlimited"):
        respx.get(f"https://img.test/{name}.jpg").mock(return_value=httpx.Response(200, content=name.encode()))
    rec = FakeRecognizer({
        b"lotus": [Recognition("b-lotus", 0.86, probs("lea", 0.9), None)],
        b"weak": [Recognition("a-bolt", 0.6, probs("lea", 0.9), None)],
        b"unlimited": [Recognition("u-bolt", 0.9, probs("2ed", 0.95), None)],
    })
    PhotoMarket.profile_calls = 0
    stats = photo_scan(settings, db, rec)
    assert stats["photo_checked"] == 3 and stats["errors"] == 0
    with db.session() as s:
        lot = s.get(Listing, "fake:p1")
        assert lot.photo_scan and "photo_match" in lot.flags
        [m] = lot.matches
        assert m.source == "vision" and m.card.id == "a-lotus"  # edition head picked the Alpha printing
        assert lot.match_count == 1 and lot.est_total_value == 30000
        assert lot.seller_name == "Seller u1" and lot.seller_info["rating"] == 4.5
        assert s.get(Listing, "fake:p2").matches == [] and s.get(Listing, "fake:p2").match_count == 0
        p3 = s.get(Listing, "fake:p3")
        assert p3.matches == [] and not any(f.startswith("vision:") for f in p3.flags)
        assert s.get(ImageScan, "https://img.test/lotus.jpg").model == "test-model"
    assert rec.calls == 3 and PhotoMarket.profile_calls == 1  # p3 isn't shown, so no profile lookup

    # rescan: unchanged ads are skipped; recognitions are cached per photo for rematches
    photo_scan(settings, db, rec)
    assert rec.calls == 3


@respx.mock
def test_text_scan_skips_photo_queries_without_model(settings, db):
    REGISTRY["fake"] = PhotoMarket
    settings.marketplaces = {"fake": MarketplaceConfig(use_search_points=False, delay_seconds=0)}
    settings.games["pokemon"].enabled = False
    settings.games["mtg"].queries, settings.games["mtg"].photo_queries = [], ["cartas magic"]
    assert Scanner(settings, db).run()["fetched"] == 0


@respx.mock
def test_wallapop_seller_profile(settings):
    respx.get("https://api.wallapop.com/api/v3/users/abc").mock(return_value=httpx.Response(200, json={
        "id": "abc", "micro_name": "Álvaro V.", "url_share": "https://www.wallapop.com/user/alvaro-1?_pid=wi",
        "register_date": 1416957464000, "is_top_profile": True, "seller_type": {"type": "Private", "verified": True}}))
    respx.get("https://api.wallapop.com/api/v3/users/abc/stats").mock(return_value=httpx.Response(200, json={
        "counters": [{"type": "publish", "value": 9}, {"type": "reviews", "value": 59}, {"type": "sold", "value": 53}],
        "rating_average": 5.0}))
    w = Wallapop(settings, settings.marketplaces["wallapop"])
    assert w.seller_profile("abc") == {
        "name": "Álvaro V.", "url": "https://www.wallapop.com/user/alvaro-1", "rating": 5.0, "reviews": 59,
        "sold": 53, "on_sale": 9, "top_profile": True, "verified": True, "type": "Private", "since": "2014-11-25"}
    w.close()


def test_create_all_adds_new_columns(tmp_path):
    url = f"sqlite:///{tmp_path}/old.db"
    eng = create_engine(url)
    with eng.begin() as c:  # a listings table from before photo_scan / seller_info existed
        c.execute(text("CREATE TABLE listings (id VARCHAR(128) PRIMARY KEY, marketplace VARCHAR(32), title TEXT)"))
        c.execute(text("INSERT INTO listings (id, marketplace, title) VALUES ('w:1', 'wallapop', 'x')"))
    db = Database(load_settings(overrides={"database_url": url}))
    db.create_all()
    with db.engine.connect() as c:
        assert c.execute(text("SELECT photo_scan, seller_info FROM listings")).one() == (0, None)


@respx.mock
def test_dashboard_filters_source_price_and_hides_unmatched(settings, db):
    from fastapi.testclient import TestClient

    from mtg_engine.api import create_app

    for name in ("lotus", "weak", "unlimited"):
        respx.get(f"https://img.test/{name}.jpg").mock(return_value=httpx.Response(200, content=name.encode()))
    photo_scan(settings, db, FakeRecognizer({b"lotus": [Recognition("b-lotus", 0.86, probs("lea", 0.9), None)]}))
    with db.session() as s:  # an ad from a targeted search, matched by text
        s.add(Listing(id="fake:t1", marketplace="fake", external_id="t1", game="mtg", url="u", title="Bolt alpha",
                      price_eur=300.0, match_count=1, est_total_value=400.0))
        s.add(Listing(id="fake:r1", marketplace="fake", external_id="r1", game="mtg", url="u", title="Lote alpha",
                      price_eur=50.0, needs_review=True))
    c = TestClient(create_app(settings, db))
    ids = lambda q="": [i["id"] for i in c.get(f"/api/listings?{q}").json()["items"]]  # noqa: E731
    assert sorted(ids()) == ["fake:p1", "fake:t1"]  # needs-review / unmatched ads are not shown
    assert ids("source=generic") == ["fake:p1"] and ids("source=targeted") == ["fake:t1"]
    assert ids("source=generic&found=photo") == ["fake:p1"]
    assert ids("max_price=100") == ["fake:p1"] and ids("min_price=100&max_price=1000") == ["fake:t1"]
    st = c.get("/api/stats").json()
    assert st["matched_generic"] == 1 and st["photo_finds"] == 1 and st["max_price_eur"] == 300.0


def _analyse_one(settings, db, raw, rec):
    from mtg_engine.pipeline import analyse, build_matcher, ingest

    with db.session() as s:
        listing, _, _ = ingest(s, settings, raw, "mtg")
        s.flush()
        analyse(s, settings, listing, build_matcher(s, settings, "mtg"), rec)
        return listing.match_count, sorted(listing.flags), [(m.card.name, m.confidence) for m in listing.matches]


@respx.mock
def test_reported_false_positives(settings, db):
    """Real cases: 'Myr alfa' read as Alpha, a Korean lot, a white-bordered Stasis."""
    for name in ("modern", "korean", "stasis_white", "stasis_vague"):
        respx.get(f"https://img.test/{name}.jpg").mock(return_value=httpx.Response(200, content=name.encode()))
    rec = FakeRecognizer({
        b"modern": [Recognition("a-fog", 0.29, probs("3ed", 0.8), None)],          # nothing like an old card
        b"korean": [Recognition("b-bolt", 0.72, probs("leb", 0.85), None)],        # FBB Korean looks like Beta
        b"stasis_white": [Recognition("b-ritual", 0.80, probs("2ed", 0.63), None)],  # white border, confident
        b"stasis_vague": [Recognition("a-ritual", 0.76, probs("2ed", 0.41), None)],  # same card, vaguer photo
    })
    # 1. edition keyword inside a modern card name; photos show nothing old
    n, flags, _ = _analyse_one(settings, db, RawListing(
        "fake", "r1", "u", "Lote de 8 cartas Magic", "- Dark Ritual - Myr alfa - Mago de guerra", 4.5,
        images=["https://img.test/modern.jpg"]), rec)
    assert n == 0 and "photos_dont_match" in flags
    # 2. Korean lot: excluded whatever the photos say
    n, flags, _ = _analyse_one(settings, db, RawListing(
        "fake", "r2", "u", "Lote cartas Magic coreano", "Lightning Bolt beta", 12.0,
        images=["https://img.test/korean.jpg"]), rec)
    assert n == 0 and "excluded" in flags and rec.calls == 1  # no photo downloads for excluded ads
    # 3. one confident white-border photo vetoes the card even if another photo is vague
    n, flags, matches = _analyse_one(settings, db, RawListing(
        "fake", "r3", "u", "Magic Dark Ritual carta", "", 10.0,
        images=["https://img.test/stasis_white.jpg", "https://img.test/stasis_vague.jpg"]), rec)
    assert n == 0 and "vision:white_border" in flags and all(c < 0.1 for _, c in matches)


def test_recheck_rejects_untouched_deal(settings, db):
    from mtg_engine.agents.workflow import maybe_close_deal
    from mtg_engine.models import Deal

    with db.session() as s:
        for lid, stage in (("fake:d1", "new"), ("fake:d2", "contacted")):
            s.add(Listing(id=lid, marketplace="fake", external_id=lid, game="mtg", url="u", title="x", match_count=0))
            s.add(Deal(listing_id=lid, stage=stage))
        s.flush()
        assert maybe_close_deal(s, s.get(Listing, "fake:d1")).stage == "rejected"
        assert maybe_close_deal(s, s.get(Listing, "fake:d2")) is None  # humans already involved


@respx.mock
def test_strong_claims_and_collectors_edition_are_not_hidden(settings, db):
    """Real cases hidden by the first version of the photo rules: slabs and Beta read as CE."""
    for name in ("slab", "beta_as_ce"):
        respx.get(f"https://img.test/{name}.jpg").mock(return_value=httpx.Response(200, content=name.encode()))
    rec = FakeRecognizer({
        b"slab": [Recognition("b-lotus", 0.34, probs("leb", 0.55), None)],       # PSA slab: barely similar
        b"beta_as_ce": [Recognition("b-bolt", 0.85, probs("ced", 0.85), None)],  # black border, read as CE
    })
    n, flags, _ = _analyse_one(settings, db, RawListing(
        "fake", "s1", "u", "Black Lotus Alpha PSA 10", "", 50000.0, images=["https://img.test/slab.jpg"]), rec)
    assert n == 1 and "photos_dont_match" not in flags
    n, flags, _ = _analyse_one(settings, db, RawListing(
        "fake", "s2", "u", "Lightning Bolt Beta", "", 300.0, images=["https://img.test/beta_as_ce.jpg"]), rec)
    assert n == 1 and not any(f.startswith("vision:") for f in flags)


@respx.mock
def test_photo_only_alpha_or_beta_unsure_values_cheaper(settings, db):
    respx.get("https://img.test/split.jpg").mock(return_value=httpx.Response(200, content=b"split"))
    split = {"lea": 0.42, "leb": 0.40, "2ed": 0.06, "3ed": 0.04, "ced": 0.05, "other": 0.03}
    rec = FakeRecognizer({b"split": [Recognition("u-bolt", 0.8, split, None)]})  # gallery hit: an Unlimited scan
    settings.games["mtg"].photo_queries = ["cartas magic"]
    REGISTRY["fake"] = PhotoMarket
    settings.marketplaces = {"fake": MarketplaceConfig(use_search_points=False, delay_seconds=0)}
    settings.games["pokemon"].enabled = False
    PHOTO_ADS_SPLIT = [RawListing("fake", "q1", "u", "Cartas magic", "", 5.0, images=["https://img.test/split.jpg"])]
    PhotoMarket.search = lambda self, q, point=None: iter(PHOTO_ADS_SPLIT)
    try:
        Scanner(settings, db, rec).run(photo_queries=True)
    finally:
        PhotoMarket.search = lambda self, q, point=None: iter(PHOTO_ADS)
    with db.session() as s:
        [m] = s.get(Listing, "fake:q1").matches
        assert m.card.id == "b-bolt" and m.edition_ambiguous and m.confidence == round(0.8 * 0.82, 3)


@respx.mock
def test_photo_only_needs_all_photos_to_agree(settings, db):
    """Real case: a modern Counterspell (original art) passed on one crop, other photos disagreed."""
    for name in ("good", "vague1", "vague2"):
        respx.get(f"https://img.test/{name}.jpg").mock(return_value=httpx.Response(200, content=name.encode()))
    ab = lambda x: {"lea": x / 2, "leb": x / 2, "2ed": (1 - x) / 4, "3ed": (1 - x) / 4, "ced": (1 - x) / 2, "other": 0.0}  # noqa: E731
    rec = FakeRecognizer({b"good": [Recognition("a-bolt", 0.74, ab(0.77), None)],
                          b"vague1": [Recognition("a-bolt", 0.75, ab(0.62), None)],
                          b"vague2": [Recognition("a-bolt", 0.69, ab(0.58), None)]})
    from mtg_engine.vision.service import apply_vision

    with db.session() as s:
        listing = Listing(id="fake:c1", marketplace="fake", external_id="c1", game="mtg", url="u", title="Cartas magic",
                          images=["https://img.test/good.jpg", "https://img.test/vague1.jpg", "https://img.test/vague2.jpg"])
        s.add(listing)
        s.flush()
        assert "photo_match" not in apply_vision(s, settings, listing, rec, text_evidence=False)
        assert listing.matches == []


@respx.mock
def test_paid_marketplace_runs_only_generic_photo_queries_first(settings, db):
    seen = []

    class Recorder(Marketplace):
        name = "fake"

        def search(self, query, point=None):
            seen.append(query)
            return iter(())

    REGISTRY["fake"] = Recorder
    settings.marketplaces = {"fake": MarketplaceConfig(use_search_points=False, delay_seconds=0, query_kinds=["photo"])}
    settings.games["pokemon"].enabled = False
    settings.games["mtg"].queries, settings.games["mtg"].photo_queries = ["mtg alpha"], ["cartas magic", "lote magic"]
    Scanner(settings, db, FakeRecognizer({})).run()
    assert seen == ["cartas magic", "lote magic"]
    seen.clear()
    settings.marketplaces["fake"].query_kinds = ["photo", "text"]
    Scanner(settings, db, FakeRecognizer({})).run()
    assert seen == ["cartas magic", "lote magic", "mtg alpha"]  # generic first


@respx.mock
def test_photo_failure_reason_is_flagged(settings, db):
    from mtg_engine.vision.service import apply_vision

    respx.get("https://img.test/refused.jpg").mock(return_value=httpx.Response(403))
    respx.get("https://img.test/odd.jpg").mock(return_value=httpx.Response(200, content=b"x", headers={"content-type": "image/avif"}))

    class Breaks(FakeRecognizer):
        def recognize_image(self, image_bytes):
            raise OSError("cannot identify image file")

    with db.session() as s:
        listing = Listing(id="fake:e1", marketplace="fake", external_id="e1", game="mtg", url="u", title="Cartas magic",
                          images=["https://img.test/refused.jpg", "https://img.test/odd.jpg"])
        s.add(listing)
        s.flush()
        flags = apply_vision(s, settings, listing, Breaks({}), text_evidence=False)
    assert {"photo_unavailable", "photo_error:http_403", "photo_error:decode:image/avif"} <= set(flags)


@respx.mock
def test_incremental_scan_keeps_photo_results_on_price_change(settings, db):
    respx.get("https://img.test/lotus.jpg").mock(return_value=httpx.Response(200, content=b"lotus"))
    rec = FakeRecognizer({b"lotus": [Recognition("b-lotus", 0.86, probs("lea", 0.9), None)]})
    photo_scan(settings, db, rec)
    assert rec.calls >= 1
    calls = rec.calls
    old = PHOTO_ADS[0]
    PHOTO_ADS[0] = RawListing(old.marketplace, old.external_id, old.url, old.title, old.description, 2000.0,
                              seller_id=old.seller_id, images=old.images)
    try:
        stats = photo_scan(settings, db, rec)
    finally:
        PHOTO_ADS[0] = old
    assert rec.calls == calls and stats["updated"] == 1 and stats["photo_checked"] == 0  # no photo work
    with db.session() as s:
        lot = s.get(Listing, "fake:p1")
        assert lot.price_eur == 2000.0 and lot.match_count == 1 and "photo_match" in lot.flags
        assert lot.value_ratio == round(30000 / 2000, 3)


def test_bargains_are_not_penalised(settings, db):
    """A Black Lotus for 50 EUR is exactly what we hunt: no demotion for a huge value/price ratio."""
    n, flags, matches = _analyse_one(settings, db, RawListing(
        "fake", "b1", "u", "Cartas viejas", "Black Lotus beta y otras", 50.0), FakeRecognizer({}))
    assert n == 1 and "implausible_price" not in flags and matches[0][0] == "Black Lotus"


@respx.mock
def test_fast_rematch_keeps_photo_vetoes(settings, db):
    """Regression: re-applying rules from stored readings must not undo a white-border veto."""
    from mtg_engine.pipeline import rematch_all

    respx.get("https://img.test/white.jpg").mock(return_value=httpx.Response(200, content=b"white"))
    rec = FakeRecognizer({b"white": [Recognition("b-ritual", 0.85, probs("2ed", 0.7), None)]})
    n, flags, _ = _analyse_one(settings, db, RawListing(
        "fake", "w1", "u", "Dark Ritual Beta", "", 30.0, images=["https://img.test/white.jpg"]), rec)
    assert n == 0 and "vision:white_border" in flags
    calls = rec.calls
    rematch_all(settings, db, rec, keep_photos=True)
    with db.session() as s:
        listing = s.get(Listing, "fake:w1")
        assert listing.match_count == 0 and "vision:white_border" in listing.flags
    assert rec.calls == calls  # nothing downloaded or recognised again


def test_seller_with_proxy_ads_is_excluded(settings, db):
    rec = FakeRecognizer({})
    _analyse_one(settings, db, RawListing("fake", "x1", "u", "Cartas Magic", "3€ cada una P r x y - Black Lotus beta",
                                          3.0, seller_id="proxyseller"), rec)
    n, flags, _ = _analyse_one(settings, db, RawListing("fake", "x2", "u", "Balance. Magic. Mtg", "Black Lotus beta 15€",
                                                        15.0, seller_id="proxyseller"), rec)
    assert n == 0 and "seller_sells_proxies" in flags


def test_import_merges_partial_records(settings, db):
    """A gallery export has no description/location: importing it must not wipe what we know."""
    from mtg_engine.marketplaces.milanuncios import parse_listing_item
    from mtg_engine.pipeline import import_listings

    full = RawListing("milanuncios", "611890139", "u", "cartas Magic", "Lote de 350 cartas", 30.0,
                      city="Elche", region="Alicante", seller_name="Vendedor", images=["https://img.test/a.jpg"])
    import_listings(settings, db, None, [full], "mtg", photo=False)
    gallery = parse_listing_item({"external_id": "611890139", "url": "u", "title": "cartas Magic", "price_text": "35 €",
                                  "images": ["https://images.milanuncios.com/api/v1/ma-ad-media-pro/images/x",
                                             "https://images.milanuncios.com/api/v1/ma-ad-media-pro/images/y"]})
    stats = import_listings(settings, db, None, [gallery], "mtg", photo=False, all_images=True)
    assert stats["fetched"] == 1
    with db.session() as s:
        listing = s.get(Listing, "milanuncios:611890139")
        assert (listing.description, listing.city, listing.region, listing.seller_name) == (
            "Lote de 350 cartas", "Elche", "Alicante", "Vendedor")
        assert listing.price_eur == 35.0 and len(listing.images) == 2 and listing.images[0].endswith("?rule=hw545")
