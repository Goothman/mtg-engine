"""Find target cards mentioned in an ad's title/description.

Heuristics (tuned for Spanish second-hand ads):
  * Card names are matched on word boundaries in accent/punctuation-normalised text,
    longest name first, so "Wall of Stone" wins over shorter overlaps.
  * Edition evidence comes from configured keywords. Keywords written in UPPERCASE
    in the config (e.g. "LEA") are matched case-sensitively, because "lea" is also a
    common Spanish verb.
  * Generic names ("Fog", "Island", "Clone"...) need edition evidence to count.
  * Card in several target sets and the ad names both or neither -> use the cheapest
    printing and flag `edition_ambiguous`.
  * No edition evidence at all -> very likely a modern reprint: low confidence,
    excluded from total value but kept for review.
  * Red-flag words (proxy, réplica, unlimited, other printings...) halve the confidence.
  * SEO keyword stuffing ("Alpha. Beta. MTG. Old. Mox. Black Lotus." or
    "Modern Commander Legacy Alpha Beta Foil...") is detected and removed before matching
    (found on real Wallapop ads, 2026-10-28).
  * Generic names only count from the title or from a sentence that also names the edition.
  * Edition named in the title wins over editions mentioned in the description.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import GameConfig
from ..text import normalize, strip_accents

GENERIC_NAMES = {
    "plains", "island", "swamp", "mountain", "forest", "fog", "fear", "lure", "clone", "blessing",
    "berserk", "fastbond", "regeneration", "flight", "feedback", "fireball", "earthquake", "tranquility",
    "sacrifice", "hurricane", "deathlace", "terror", "weakness", "giant growth", "holy strength",
    "unholy strength", "paralyze", "purelace", "camouflage", "smoke", "web", "twiddle", "counterspell",
    "disintegrate", "shatter", "flashfires", "tsunami", "armageddon", "wrath of god", "animate dead",
    "instill energy", "invisibility", "jump", "burrowing", "firebreathing", "lifetap", "lifelace",
    "consecrate land", "conversion", "creature bond", "darkpact", "demonic attorney", "evil presence",
    "farmstead", "guardian angel", "island sanctuary", "black lotus",
}
GENERIC_NAMES.discard("black lotus")  # specific enough on its own

# "4x Name" binds forward, "Name x4" binds backward; so "Bolt x4, 2x Ritual" is unambiguous.
_QTY_BEFORE = re.compile(r"(?:^|\s)(\d{1,2})\s?x\s*$")
_QTY_AFTER = re.compile(r"^\s*x\s?(\d{1,2})(?:\s|$)")


@dataclass
class CardRef:
    id: str
    name: str
    name_norm: str
    set_code: str
    price_eur: float | None
    rarity: str | None = None
    type_line: str | None = None
    is_basic: bool = False


@dataclass
class TextMatch:
    card: CardRef
    quantity: int
    confidence: float
    matched_text: str
    edition_ambiguous: bool


@dataclass
class MatchResult:
    matches: list[TextMatch] = field(default_factory=list)
    editions: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    needs_review: bool = False


def _kw_pattern(kw: str) -> tuple[re.Pattern[str], bool]:
    case_sensitive = kw.isupper()
    text = strip_accents(kw) if case_sensitive else normalize(kw)
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(text)}(?![A-Za-z0-9])"), case_sensitive


# asking price is a placeholder ("precio (x10)", "consultar precio"...): seen on real ads
_PRICE_PLACEHOLDER = re.compile(
    r"precio\s*\(?x\s?\d+|x\s?\d+\)?\s*(?:el\s+)?precio|precio real|consultar (?:el )?precio|"
    r"preguntar (?:el )?precio|precio a consultar|precio por (?:privado|mensaje|mp)|no es el precio"
)
_SENT_SPLIT = re.compile(r"(?<!\d)[.;|!?¡¿](?!\d)|\n")


def _is_tag(item: str, tags: set[str]) -> bool:
    return normalize(item) in tags


def split_stuffing(description: str, tags: set[str]) -> tuple[list[str], bool]:
    """Split a description into sentences and drop SEO tag lists that contain edition words.

    Returns (kept_sentences, removed_any_with_tags)."""
    segs = [x.strip() for x in _SENT_SPLIT.split(description or "")]
    segs = [x for x in segs if x]
    drop = [False] * len(segs)
    for i, seg in enumerate(segs):
        words = seg.split()
        parts = [p.strip() for p in seg.split(",") if p.strip()]
        listy = len(parts) >= 5 and sum(len(p.split()) for p in parts) / len(parts) <= 3
        alpha_words = [w for w in words if w[:1].isalpha()]
        capsy = len(alpha_words) >= 6 and sum(w[0].isupper() for w in alpha_words) / len(alpha_words) >= 0.85
        has_tag = any(_is_tag(p, tags) for p in parts) or any(_is_tag(w, tags) for w in words)
        if (listy or capsy) and has_tag:
            drop[i] = True
    # runs of >= 4 consecutive 1-3 word fragments ("Alpha. Beta. MTG. Old.") containing a tag
    i = 0
    while i < len(segs):
        j = i
        while j < len(segs) and len(segs[j].split()) <= 3:
            j += 1
        if j - i >= 4 and any(_is_tag(x, tags) for x in segs[i:j]):
            for k in range(i, j):
                drop[k] = True
        i = max(j, i + 1)
    kept = [x for x, d in zip(segs, drop) if not d]
    return kept, any(drop)


class TextMatcher:
    def __init__(self, cards: list[CardRef], game: GameConfig):
        self.game = game
        self.by_name: dict[str, dict[str, CardRef]] = {}
        for c in cards:
            if c.set_code in game.target_codes:
                # several printings of one name in a set (e.g. Alpha's two Islands): keep the cheapest
                sets = self.by_name.setdefault(c.name_norm, {})
                prev = sets.get(c.set_code)
                if prev is None or (c.price_eur or float("inf")) < (prev.price_eur or float("inf")):
                    sets[c.set_code] = c
        names = sorted(self.by_name, key=len, reverse=True)
        self.name_re = (
            re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(n) for n in names) + r")(?![a-z0-9])")
            if names
            else None
        )
        self.edition_res = {t.set_code: [_kw_pattern(k) for k in t.keywords] for t in game.targets}
        self.tags = {normalize(k) for t in game.targets for k in t.keywords} | {normalize(t.label) for t in game.targets}
        # dedupe on the normalised form ("replica" and "réplica" are the same flag)
        self.flag_res = list({normalize(f): (normalize(f), _kw_pattern(f)[0]) for f in game.red_flags}.values())

    def _passes_filters(self, c: CardRef) -> bool:
        f = self.game.filters
        if f.rarities and (c.rarity or "").lower() not in {r.lower() for r in f.rarities}:
            return False
        if f.type_contains and not any(t.lower() in (c.type_line or "").lower() for t in f.type_contains):
            return False
        if f.exclude_basic_lands and c.is_basic:
            return False
        return True

    @staticmethod
    def _quantity(text: str, start: int, end: int) -> int:
        before = text[max(0, start - 8) : start]
        after = text[end : end + 8]
        for m in (_QTY_BEFORE.search(before), _QTY_AFTER.search(after)):
            if m:
                q = int(next(g for g in m.groups() if g))
                if 1 <= q <= 40:
                    return q
        return 1

    def _editions(self, raw: str) -> list[str]:
        norm, cased = normalize(raw), strip_accents(raw)
        return [code for code, pats in self.edition_res.items() if any(p.search(cased if cs else norm) for p, cs in pats)]

    def match(self, title: str, description: str = "") -> MatchResult:
        kept, stuffed = split_stuffing(description, self.tags)
        raw = "\n".join([title, *kept])
        norm = normalize(raw)
        res = MatchResult()
        res.editions = self._editions(raw)
        title_editions = self._editions(title)
        title_norm = normalize(title)
        res.flags = [name for name, p in self.flag_res if p.search(norm)]
        if stuffed:
            res.flags.append("keyword_stuffing")
        if _PRICE_PLACEHOLDER.search(normalize(f"{title} {description}")):
            res.flags.append("price_placeholder")
        # sentences that name an edition: generic card names are only trusted there (or in the title)
        edition_sents = [strip_accents(x) for x in kept if self._editions(x)]

        found: dict[str, tuple[int, str]] = {}  # name_norm -> (max qty, matched text)
        if self.name_re:
            for m in self.name_re.finditer(norm):
                name = m.group(1)
                qty = self._quantity(norm, m.start(), m.end())
                prev = found.get(name)
                if not prev or qty > prev[0]:
                    found[name] = (qty, norm[max(0, m.start() - 8) : m.end() + 8].strip())

        penalty = 0.5 if [f for f in res.flags if f not in ("keyword_stuffing", "price_placeholder")] else 1.0
        word = lambda n, text: re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", text)  # noqa: E731
        for name, (qty, snippet) in found.items():
            printings = self.by_name[name]
            generic = name in GENERIC_NAMES or len(name) < 5
            in_title = bool(word(name, title_norm))
            if generic:
                # in the description a generic name must be capitalised like a card name
                # ("Island x10" yes, "la web de CGC" no) and share a sentence with the edition
                proper = re.compile(rf"(?<![A-Za-z0-9]){re.escape(strip_accents(printings[next(iter(printings))].name))}(?![A-Za-z0-9])")
                if not (res.editions and (in_title or any(proper.search(x) for x in edition_sents))):
                    continue
            # an edition named in the title beats editions mentioned in the description
            editions = title_editions if (in_title and len(title_editions) == 1) else res.editions
            named = [printings[c] for c in editions if c in printings]
            if len(named) == 1:
                card, conf, ambiguous = named[0], 0.9, False
            else:
                pool = named or list(printings.values())
                card = min(pool, key=lambda c: c.price_eur if c.price_eur is not None else float("inf"))
                ambiguous = len(pool) > 1
                conf = 0.75 if named else (0.55 if res.editions else 0.2)
            if generic:
                conf -= 0.1
            if not self._passes_filters(card):
                continue
            min_val = self.game.filters.min_card_value_eur
            if min_val and (card.price_eur or 0) < min_val:
                continue
            res.matches.append(TextMatch(card, qty, round(conf * penalty, 3), snippet, ambiguous))

        res.matches.sort(key=lambda m: (m.card.price_eur or 0) * m.quantity, reverse=True)
        res.needs_review = bool(res.editions) and not any(m.confidence >= 0.5 for m in res.matches)
        return res
