"""Second check on a photo find: read the card's text (OCR) and compare it with each edition's wording.

The picture model tells Alpha/Beta from look-alikes by border and corners; the words tell the rest:
  * later editions reworded most cards ("must be discarded" in Beta, "is destroyed" in Revised,
    "Destroy target ..." in 4th): text that reads like a later edition rules Alpha/Beta out;
  * Alpha/Beta were printed in English only: rules text in Spanish, Italian, French, German or
    Portuguese rules them out;
  * Alpha/Beta have no copyright line: "Wizards of the Coast" / "(c) 1995" rules them out.
Unreadable text (small cards in a lot photo, glare) is "unclear" and changes nothing: a find is
only ever removed on positive evidence. Wording per edition comes from MTGJSON
(scripts/edition_texts.py -> mtg_engine/data/edition_texts.json.gz).
"""
from __future__ import annotations

import difflib
import gzip
import json
import logging
import os
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)
# tesseract uses OpenMP threads; several readers at once (thread pools) then stall each other
# (8 photos: >10 min instead of 3 s). One thread per reader; parallelism comes from the pool.
os.environ.setdefault("OMP_THREAD_LIMIT", "1")

DATA = Path(__file__).resolve().parents[1] / "data" / "edition_texts.json.gz"
TARGETS = ("lea", "leb")
# editions worded like Alpha/Beta: the text can't tell these apart (borders and corners can)
SAME_WORDING = {"2ed", "ced", "cei"}
VERSION = "1"  # bump when the decision rules change: cached verdicts are redone

# Words that mark a language. Only words that are not also English or shared between the
# lists, so one stray token doesn't decide; accents are stripped like the OCR output.
_LANG_WORDS = {
    "es": "el los las del que criatura criaturas objetivo hasta turno jugador jugadores una cada puede dano "
          "con por sus se al cuando tierra tierras carta cartas mazo mano cementerio destruye robar fuerza "
          "resistencia invocar conjuro instantaneo encantamiento artefacto pierde gana mientras otra",
    "it": "il gli della delle dei una creatura creature bersaglio fino alla fine turno giocatore ogni puo danno "
          "che per quando terra terre carta carte grimorio mano cimitero distruggi pesca forza costituzione "
          "evoca stregoneria istantaneo incantesimo artefatto perde guadagna mentre",
    "fr": "les des une creature creatures cible jusqu fin tour joueur chaque peut degats vous votre quand "
          "terrain terrains carte cartes bibliotheque cimetiere detruisez piochez endurance invocation "
          "rituel ephemere enchantement artefact perd gagne tant que",
    "de": "der das den dem des eine einer ein kreatur kreaturen bis ende zuges spieler jede jeder kann schaden "
          "und ist nicht von auf wenn land lander karte karten bibliothek hand friedhof zerstore ziehe starke "
          "widerstandskraft spontanzauber hexerei verzauberung artefakt verliert erhalt solange",
    "pt": "os uma criatura criaturas alvo ate turno jogador cada pode dano que nao quando terreno terrenos "
          "carta cartas grimorio mao cemiterio destrua compre forca resistencia feitico instantaneo encantamento "
          "artefato perde ganha enquanto",
}
_EN_WORDS = set("the of to target creature creatures card cards and you your until end turn each player may all "
                "any is or be from play when with this its damage does cannot must are into opponent land lands "
                "library hand graveyard destroy destroyed draw power toughness summon sorcery instant enchantment "
                "artifact loses gains while discard discarded counter spell".split())

# English words that are also words in the lists above ("main", "final", "non-wall"...)
_EN_LOOKALIKES = {"die", "final", "main", "force", "non", "est", "hand", "land", "per", "tour", "fin", "del"}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def _tokens(s: str) -> list[str]:
    return re.findall(r"[a-z]{3,}", _norm(s))


@lru_cache(maxsize=1)
def _lang_words() -> dict[str, set[str]]:
    sets = {k: set(v.split()) for k, v in _LANG_WORDS.items()}
    shared = {w for k, ws in sets.items() for w in ws if any(w in o for j, o in sets.items() if j != k)}
    return {k: ws - shared - _EN_WORDS - _EN_LOOKALIKES for k, ws in sets.items()}


@lru_cache(maxsize=1)
def wording() -> dict:
    if not DATA.exists():
        return {}
    with gzip.open(DATA, "rt", encoding="utf-8") as fh:
        return json.load(fh)["cards"]


def available() -> bool:
    return bool(wording()) and shutil.which("tesseract") is not None


@dataclass
class Verdict:
    decision: str  # "contradicts" | "confirms" | "unclear"
    reason: str = ""  # e.g. "wording:4ed", "language:es", "copyright"
    evidence: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"decision": self.decision, "reason": self.reason, "evidence": self.evidence, "v": VERSION}


def _found(word: str, ocr: set[str]) -> bool:
    if word in ocr:
        return True
    if len(word) < 5:
        return False
    return any(abs(len(o) - len(word)) <= 2 and difflib.SequenceMatcher(None, word, o).ratio() >= 0.8 for o in ocr)


def judge(ocr_text: str, card_name: str) -> Verdict:
    """Decide from text read on the card whether it can be an English Alpha/Beta printing."""
    words = _tokens(ocr_text)
    ocr = set(words)
    ev: dict = {"ocr_words": len(words)}
    if not words:
        return Verdict("unclear", "no text", ev)

    # copyright line: never on Alpha/Beta
    norm = _norm(ocr_text)
    if re.search(r"wizards\s*of\s*the|\bwizards\b|\(c\)\s*19|©\s*19", norm):
        return Verdict("contradicts", "copyright", ev)

    # language
    hits = {lang: sorted(w for w in ocr if w in ws) for lang, ws in _lang_words().items()}
    en = sorted(w for w in ocr if w in _EN_WORDS)
    lang, found = max(hits.items(), key=lambda kv: len(kv[1]))
    ev["language_words"] = {"en": en[:12], lang: found[:12]}
    if len(found) >= 3 and len(found) > len(en):
        return Verdict("contradicts", f"language:{lang}", ev)

    # wording
    card = wording().get(card_name)
    ab = next((card["en"][s] for s in TARGETS if card and s in card["en"]), None)
    if ab is None:
        return Verdict("unclear", "no reference wording", ev)
    name = set(_tokens(card_name))
    ab_tokens = set(_tokens(f"{ab['type']} {ab['text']}")) - name
    # Readable: enough of the words every edition shares were read to trust what's missing.
    # Later edition: its own words read and none of the Alpha/Beta-only ones (often a single word
    # differs: "must be discarded" -> "is destroyed"). Confirmed: the reverse, for every edition.
    shared_read = sum(_found(t, ocr) for t in ab_tokens & set().union(*(
        set(_tokens(f"{w['type']} {w['text']}")) for s, w in card["en"].items() if s not in TARGETS)))
    best_other, best_hits, ab_hits_vs = None, 0, []
    for s, w in card["en"].items():
        if s in TARGETS or s in SAME_WORDING or not (w["text"] or w["type"]):
            continue
        other = set(_tokens(f"{w['type']} {w['text']}")) - name
        only_other, only_ab = other - ab_tokens, ab_tokens - other
        if not only_other and not only_ab:
            continue  # worded the same: no information
        h_other = sum(_found(t, ocr) for t in only_other)
        h_ab = sum(_found(t, ocr) for t in only_ab)
        ab_hits_vs.append((s, h_ab, h_other))
        if h_ab == 0 and h_other >= max(1, 0.5 * len(only_other)) and h_other > best_hits:
            best_other, best_hits = s, h_other
    # edition: [Alpha/Beta-only words read, its own words read]
    ev["wording_hits"] = {s: [a, o] for s, a, o in ab_hits_vs}
    ev["shared_words_read"] = shared_read
    if shared_read < 2:
        return Verdict("unclear", "not enough text read", ev)
    if best_other:
        return Verdict("contradicts", f"wording:{best_other}", ev)
    if ab_hits_vs and all(a >= 1 and o == 0 for _, a, o in ab_hits_vs):
        return Verdict("confirms", "wording:alpha/beta", ev)
    return Verdict("unclear", "not enough text read", ev)


# ------------------------------------------------------------------------------ OCR


def _crop(img: np.ndarray, box) -> np.ndarray:
    if box is None:
        return img
    x, y, w, h = box
    m = int(0.06 * max(w, h))
    H, W = img.shape[:2]
    return img[max(0, y - m): min(H, y + h + m), max(0, x - m): min(W, x + w + m)]


def read_text(img_bgr: np.ndarray, box=None) -> str:
    """OCR the card at `box` (whole image if None). Engine: tesseract (default) or PaddleOCR
    (TEXTCHECK_OCR=paddle, pip install paddlepaddle paddleocr), being compared in
    ml/eval_textcheck.py."""
    if os.environ.get("TEXTCHECK_OCR", "tesseract") == "paddle":
        return _read_paddle(_crop(img_bgr, box))
    return _read_tesseract(img_bgr, box)


@lru_cache(maxsize=1)
def _paddle():
    from paddleocr import PaddleOCR

    return PaddleOCR(lang="en", use_doc_orientation_classify=True, use_doc_unwarping=False,
                     use_textline_orientation=True)


def _read_paddle(crop: np.ndarray) -> str:
    """PaddleOCR 3.x: the page orientation model turns the card upright (0/90/180/270)."""
    import cv2

    long_side = max(crop.shape[:2])
    if long_side < 1200:
        f = 1200 / long_side
        crop = cv2.resize(crop, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
    words = []
    for res in _paddle().predict(crop):
        r = res.json.get("res", res.json) if hasattr(res, "json") else res
        for text, score in zip(r.get("rec_texts", []), r.get("rec_scores", [])):
            if score >= 0.5:
                words.append(text)
    return " ".join(words)


def _read_tesseract(img_bgr: np.ndarray, box=None) -> str:
    """Tries the four orientations; returns the reading with the most confident words."""
    import cv2
    import pytesseract

    crop = _crop(img_bgr, box)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    long_side = max(gray.shape)
    if long_side < 1600:  # tesseract wants ~30 px letters; a card is ~40 lines tall
        f = 1600 / long_side
        gray = cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
    best, best_score = "", -1.0
    for rot in (None, cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_180, cv2.ROTATE_90_COUNTERCLOCKWISE):
        view = gray if rot is None else cv2.rotate(gray, rot)
        d = pytesseract.image_to_data(view, config="--psm 11", output_type=pytesseract.Output.DICT)
        words = [(w, float(c)) for w, c in zip(d["text"], d["conf"]) if len(w.strip()) >= 3 and float(c) > 0]
        score = sum(c for w, c in words if re.fullmatch(r"[A-Za-zÀ-ÿ'.,:;()-]+", w.strip())) / 100
        if score > best_score:
            best, best_score = " ".join(w for w, c in words if c >= 40), score
    return best


def check_photo(image_bytes: bytes, card_name: str, box=None, recognizer=None, card_ids: set[str] | None = None) -> Verdict:
    """Read the card named `card_name` in this photo and judge it. Without a box, the recognizer
    finds the card again (stored readings from before boxes were kept have none)."""
    import io

    import cv2
    from PIL import Image

    img = cv2.cvtColor(np.array(Image.open(io.BytesIO(image_bytes)).convert("RGB")), cv2.COLOR_RGB2BGR)
    if box is None and recognizer is not None and card_ids:
        hits = [r for r in recognizer.recognize_image(image_bytes) if r.card_id in card_ids]
        if hits:
            box = max(hits, key=lambda r: r.similarity).box
    v = judge(read_text(img, box), card_name)
    v.evidence["box"] = list(box) if box else None
    return v
