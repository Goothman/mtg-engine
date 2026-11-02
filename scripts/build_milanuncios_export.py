"""Build the one-line browser console version of scripts/milanuncios_export.src.js.

    python scripts/build_milanuncios_export.py      # writes scripts/milanuncios_export.js
"""
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
src = (HERE / "milanuncios_export.src.js").read_text(encoding="utf-8")
src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)  # block comments only: "//" also appears in URLs
line = " ".join(s.strip() for s in src.splitlines() if s.strip())
(HERE / "milanuncios_export.js").write_text(line + "\n", encoding="utf-8")
print(f"{len(line)} characters, one line")
