from mtg_engine import geo


def test_exact_coordinates_win():
    lat, lon, p, prov = geo.resolve("ES", 40.0, -3.0, "Gijón", None)
    assert (lat, lon, p, prov) == (40.0, -3.0, "exact", "Asturias")


def test_province_fallback_with_aliases():
    lat, lon, p, prov = geo.resolve("ES", None, None, None, "Vizcaya", key="x")
    assert p == "province" and prov == "Bizkaia" and abs(lat - 43.26) < 0.1


def test_unknown():
    assert geo.resolve("ES", None, None, "Atlantis", None)[2] == "none"
