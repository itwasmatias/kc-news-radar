"""Text normalization helpers used across pipeline stages."""

from __future__ import annotations

import html
import re
import unicodedata


_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]")


def clean_text(text: str | None) -> str:
    if not text:
        return ""
    t = html.unescape(text)
    t = unicodedata.normalize("NFKC", t)
    t = _WS_RE.sub(" ", t).strip()
    return t


def normalized_title(text: str | None) -> str:
    """Case-folded, punctuation-stripped title for similarity comparisons."""
    t = clean_text(text).casefold()
    t = _PUNCT_RE.sub(" ", t)
    return _WS_RE.sub(" ", t).strip()


def token_set(text: str | None) -> set[str]:
    return set(normalized_title(text).split())


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0
