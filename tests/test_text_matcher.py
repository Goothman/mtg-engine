from mtg_engine.matching import TextMatcher


def by_name(res):
    return {m.card.name: m for m in res.matches}


def test_edition_and_quantities(settings, refs):
    tm = TextMatcher(refs, settings.games["mtg"])
    res = tm.match("Lote Magic Alpha: Lightning Bolt x4, 2x Dark Ritual", "")
    assert res.editions == ["lea"]
    m = by_name(res)
    assert m["Lightning Bolt"].card.id == "a-bolt" and m["Lightning Bolt"].quantity == 4
    assert m["Dark Ritual"].card.id == "a-ritual" and m["Dark Ritual"].quantity == 2
    assert all(x.confidence == 0.9 and not x.edition_ambiguous for x in res.matches)


def test_both_editions_named_uses_cheaper_printing(settings, refs):
    res = TextMatcher(refs, settings.games["mtg"]).match("Cartas alpha y beta", "Black Lotus")
    lotus = by_name(res)["Black Lotus"]
    assert lotus.card.set_code == "leb" and lotus.edition_ambiguous and lotus.confidence == 0.75


def test_no_edition_is_low_confidence_and_generic_names_skipped(settings, refs):
    res = TextMatcher(refs, settings.games["mtg"]).match("Lightning Bolt y Fog", "baratas")
    names = by_name(res)
    assert "Fog" not in names  # generic name without edition evidence
    assert names["Lightning Bolt"].confidence < 0.5
    assert not res.needs_review


def test_lowercase_lea_is_not_alpha_but_uppercase_is(settings, refs):
    tm = TextMatcher(refs, settings.games["mtg"])
    assert tm.match("Lightning Bolt", "por favor lea la descripcion").editions == []
    assert tm.match("Lightning Bolt LEA", "").editions == ["lea"]


def test_red_flags_halve_confidence(settings, refs):
    res = TextMatcher(refs, settings.games["mtg"]).match("Black Lotus alpha proxy", "")
    assert "proxy" in res.flags
    assert by_name(res)["Black Lotus"].confidence == 0.45


def test_longest_name_and_accents(settings, refs):
    res = TextMatcher(refs, settings.games["mtg"]).match("WALL OF STONE (Alpha)", "edición limitada")
    assert "Wall of Stone" in by_name(res)


def test_needs_review_when_edition_but_no_cards(settings, refs):
    res = TextMatcher(refs, settings.games["mtg"]).match("Lote 60 cartas magic beta", "ver fotos")
    assert res.editions == ["leb"] and res.needs_review and not res.matches


def test_card_only_in_one_target_set(settings, refs):
    res = TextMatcher(refs, settings.games["mtg"]).match("Shivan Dragon alpha", "")
    m = by_name(res)["Shivan Dragon"]
    assert m.card.set_code == "leb"  # only printing we know; edition named doesn't have it


def test_filters(settings, refs):
    g = settings.games["mtg"].model_copy(deep=True)
    g.filters.min_card_value_eur = 100
    res = TextMatcher(refs, g).match("alpha Fog Wall of Stone Lightning Bolt", "")
    assert set(by_name(res)) == {"Lightning Bolt"}


# Patterns below are from real Wallapop ads (2026-10-28), shortened.
def test_keyword_stuffing_is_ignored(settings, refs):
    tm = TextMatcher(refs, settings.games["mtg"])
    r = tm.match("Lago hirviente Caldera lake Tempestad carta Magic",
                 "Carta tierra rara de la coleccion Tempestad en español. Envío por Wallapop. "
                 "Alpha. Beta. MTG. Old. Mox. Black Lotus. Land. Años 90. Borde negro.")
    assert not r.matches and "keyword_stuffing" in r.flags
    r = tm.match("Magic the Gathering: Reinos Misteriosos", "Caja de inicio Magic.\n"
                 "Magic the gathering, black lotus, mox, arabian nights, sellado, sealed, alpha, beta, collector, carta.")
    assert not r.matches
    r = tm.match("Magic Wrath of God", "Carta de Magic.\nModern Commander Legacy Alpha Beta Foil Artefacto Criatura Tierra")
    assert not r.matches


def test_real_card_list_is_not_stuffing(settings, refs):
    r = TextMatcher(refs, settings.games["mtg"]).match(
        "Lote alpha", "4x Lightning Bolt, 2x Dark Ritual, Wall of Stone, Shivan Dragon, Fog")
    names = {m.card.name for m in r.matches}
    assert {"Lightning Bolt", "Dark Ritual", "Wall of Stone"} <= names


def test_title_edition_wins_and_generic_name_needs_context(settings, refs):
    r = TextMatcher(refs, settings.games["mtg"]).match(
        "Black Lotus LEB Magic The Gathering CGC 5",
        "Edición Beta, verificable en la web de CGC. Si alguien está interesado en un Black Lotus de Alpha preguntar.")
    by = {m.card.name: m for m in r.matches}
    assert by["Black Lotus"].card.set_code == "leb" and not by["Black Lotus"].edition_ambiguous
    assert "Web" not in by and "Fog" not in by


def test_other_printing_flags(settings, refs):
    r = TextMatcher(refs, settings.games["mtg"]).match("Lightning Bolt beta", "Quinta edición, 1997")
    assert "quinta edicion" in r.flags and r.matches[0].confidence < 0.5


def test_price_placeholder_flag(settings, refs):
    r = TextMatcher(refs, settings.games["mtg"]).match("Black Lotus LEB", "Más detalles y precio (x10) negociable por mensajería")
    assert "price_placeholder" in r.flags and r.matches[0].confidence == 0.9
