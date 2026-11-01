"""Second check on photo finds: the text read on the card vs each edition's wording and language."""
import httpx
import pytest
import respx

from mtg_engine.models import Listing, TextCheck
from mtg_engine.vision import textcheck
from mtg_engine.vision.recognizer import Recognition
from mtg_engine.vision.textcheck import Verdict, judge

from test_photo_scan_sellers import FakeRecognizer, probs

needs_wording = pytest.mark.skipif(not textcheck.wording().get("Disenchant"), reason="edition wording data missing")


@needs_wording
@pytest.mark.parametrize("text, decision, reason", [
    ("Disenchant Instant Target enchantment or artifact must be discarded. Illus. Amy Weber", "confirms", "wording:alpha/beta"),
    ("Disenchant Instant Destroy target enchantment or artifact. Illus. Amy Weber", "contradicts", "wording:"),  # 4th
    ("Disenchant Instant Target enchantment or artifact is destroyed.", "contradicts", "wording:"),  # Revised
    ("Illus. Amy Weber © 1995 Wizards of the Coast, Inc.", "contradicts", "copyright"),
    ("Desencantar Destruye un encantamiento o artefacto objetivo hasta el final del turno de la criatura", "contradicts", "language:es"),
    ("Disinnesca Distruggi un incantesimo o artefatto bersaglio della creatura", "contradicts", "language:it"),
    ("Disen ant Ins nt Tar", "unclear", ""),  # unreadable: never removes a find
    ("", "unclear", ""),
])
def test_judge(text, decision, reason):
    v = judge(text, "Disenchant")
    assert v.decision == decision and v.reason.startswith(reason)


def test_english_rules_text_is_not_mistaken_for_another_language():
    v = judge("Target non-Wall creature: the final damage is dealt to the main player's hand until end of turn", "Unknown Card")
    assert not v.reason.startswith("language")


def _photo_find(settings, db, rec, url="https://img.test/dis.jpg"):
    from mtg_engine.vision.service import apply_vision

    settings.vision.text_check = True
    with db.session() as s:
        listing = Listing(id="fake:t1", marketplace="fake", external_id="t1", game="mtg", url="u",
                          title="Lote cartas magic", images=[url])
        s.add(listing)
        s.flush()
        flags = apply_vision(s, settings, listing, rec, text_evidence=False)
        return flags, [(m.card.name, m.source) for m in listing.matches]


@respx.mock
@pytest.mark.parametrize("decision, kept", [("contradicts", False), ("unclear", True), ("confirms", True)])
def test_text_check_on_photo_finds(settings, db, monkeypatch, decision, kept):
    respx.get("https://img.test/dis.jpg").mock(return_value=httpx.Response(200, content=b"dis"))
    rec = FakeRecognizer({b"dis": [Recognition("b-lotus", 0.86, probs("lea", 0.9), (10, 10, 100, 140))]})
    calls = []

    def fake_check(content, name, box, recognizer, ids):
        calls.append((name, box))
        return Verdict(decision, "wording:4ed" if decision == "contradicts" else "")

    monkeypatch.setattr(textcheck, "available", lambda: True)
    monkeypatch.setattr(textcheck, "check_photo", fake_check)
    flags, matches = _photo_find(settings, db, rec)
    assert calls == [("Black Lotus", (10, 10, 100, 140))]  # the card's place in the photo is used
    assert bool(matches) is kept
    assert ("text_alpha_beta_wording" in flags) is (decision == "confirms")
    with db.session() as s:
        assert s.get(TextCheck, ("https://img.test/dis.jpg", "Black Lotus")).result["decision"] == decision


@respx.mock
def test_text_check_is_done_once_per_photo_and_card(settings, db, monkeypatch):
    from mtg_engine.pipeline import rematch_all

    respx.get("https://img.test/dis.jpg").mock(return_value=httpx.Response(200, content=b"dis"))
    rec = FakeRecognizer({b"dis": [Recognition("b-lotus", 0.86, probs("lea", 0.9), None)]})
    calls = []
    monkeypatch.setattr(textcheck, "available", lambda: True)
    monkeypatch.setattr(textcheck, "check_photo", lambda *a: calls.append(a) or Verdict("unclear"))
    _photo_find(settings, db, rec)  # (turns the check on)
    with db.session() as s:
        s.get(Listing, "fake:t1").photo_scan = True
    rematch_all(settings, db, rec, keep_photos=True)
    assert len(calls) == 1


@respx.mock
def test_unlikely_cards_are_not_read(settings, db, monkeypatch):
    """OCR only for likely finds: a reading the model gives low Alpha/Beta odds isn't read."""
    respx.get("https://img.test/dis.jpg").mock(return_value=httpx.Response(200, content=b"dis"))
    rec = FakeRecognizer({b"dis": [Recognition("b-lotus", 0.86, probs("other", 0.9), None)]})
    calls = []
    monkeypatch.setattr(textcheck, "available", lambda: True)
    monkeypatch.setattr(textcheck, "check_photo", lambda *a: calls.append(a) or Verdict("unclear"))
    _photo_find(settings, db, rec)
    assert calls == []
