import pytest

from mtg_engine.config import load_settings
from mtg_engine.db import Database
from mtg_engine.matching import CardRef
from mtg_engine.models import CardPrinting
from mtg_engine.text import normalize

CATALOG = [
    # (id, name, set, price)
    ("a-bolt", "Lightning Bolt", "lea", 400.0),
    ("b-bolt", "Lightning Bolt", "leb", 200.0),
    ("a-ritual", "Dark Ritual", "lea", 150.0),
    ("b-ritual", "Dark Ritual", "leb", 80.0),
    ("a-lotus", "Black Lotus", "lea", 30000.0),
    ("b-lotus", "Black Lotus", "leb", 18000.0),
    ("a-fog", "Fog", "lea", 20.0),
    ("b-fog", "Fog", "leb", 10.0),
    ("a-wall", "Wall of Stone", "lea", 60.0),
    ("b-web", "Web", "leb", 15.0),
    ("b-shivan", "Shivan Dragon", "leb", 700.0),
    ("u-bolt", "Lightning Bolt", "2ed", 60.0),  # confuser set, never a target
]


@pytest.fixture
def settings():
    return load_settings(overrides={"database_url": "sqlite:///:memory:"})


@pytest.fixture
def refs():
    return [CardRef(i, n, normalize(n), s, p) for i, n, s, p in CATALOG]


@pytest.fixture
def db(settings):
    d = Database(settings)
    d.create_all()
    with d.session() as s:
        for i, n, st, p in CATALOG:
            s.add(CardPrinting(id=i, game="mtg", set_code=st, set_name=st, name=n, name_norm=normalize(n),
                               is_target=st in ("lea", "leb"), price_eur=p, price_source="test"))
    return d
