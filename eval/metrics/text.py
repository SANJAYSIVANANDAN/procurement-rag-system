"""Text comparison used by several metrics."""

import re

from rapidfuzz import fuzz

_WORD = re.compile(r"[a-z0-9]+")
OVERLAP_THRESHOLD = 0.5


def tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def jaccard(a: str, b: str) -> float:
    left, right = tokens(a), tokens(b)
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def overlaps(a: str, b: str, threshold: float = OVERLAP_THRESHOLD) -> bool:
    """Do two passages say substantially the same thing (token Jaccard at least `threshold`)?"""
    return jaccard(a, b) >= threshold


def similarity(a: str, b: str) -> float:
    """Normalised Levenshtein similarity, 0 to 1 (RAGAS's non-LLM string similarity)."""
    return float(fuzz.ratio(a, b)) / 100.0
