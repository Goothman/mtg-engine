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
  * Red-flag words (proxy, réplica, unlimited...) halve the confidence.
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
        self.flag_res = [(f, _kw_pattern(f)[0]) for f in game.red_flags]

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

    def match(self, title: str, description: str = "") -> MatchResult:
        raw = f"{title}\n{description}"
        norm = normalize(raw)
        cased = strip_accents(raw)
        res = MatchResult()

        for code, patterns in self.edition_res.items():
            if any(p.search(cased if cs else norm) for p, cs in patterns):
                res.editions.append(code)
        res.flags = [name for name, p in self.flag_res if p.search(norm)]

        found: dict[str, tuple[int, str]] = {}  # name_norm -> (max qty, matched text)
        if self.name_re:
            for m in self.name_re.finditer(norm):
                name = m.group(1)
                qty = self._quantity(norm, m.start(), m.end())
                prev = found.get(name)
                if not prev or qty > prev[0]:
                    found[name] = (qty, norm[max(0, m.start() - 8) : m.end() + 8].strip())

        penalty = 0.5 if res.flags else 1.0
        for name, (qty, snippet) in found.items():
            printings = self.by_name[name]
            generic = name in GENERIC_NAMES or len(name) < 5
            if generic and not res.editions:
                continue
            named = [printings[c] for c in res.editions if c in printings]
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
