"""Text normalisation shared by matching and catalog code."""
from __future__ import annotations

import re
import unicodedata

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def normalize(s: str | None) -> str:
    """Lowercase, strip accents/punctuation, collapse whitespace. "Æther" -> "aether"."""
    if not s:
        return ""
    s = s.replace("Æ", "Ae").replace("æ", "ae").replace("’", "'")
    s = strip_accents(s).lower().replace("'", "")
    return _NON_ALNUM.sub(" ", s).strip()
