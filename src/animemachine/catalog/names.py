"""Search-only person-name folding; never changes identity or display names."""
from __future__ import annotations

import json
import unicodedata
from functools import lru_cache
from importlib.resources import files


@lru_cache(maxsize=1)
def _character_table() -> dict[int, str]:
    payload = json.loads(files("animemachine.resources").joinpath("person-name-fold.json").read_text(encoding="utf-8"))
    return {ord(key): value for key, value in payload["mapping"].items()}


def search_name(value: str | None) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).translate(_character_table()).casefold().strip()
