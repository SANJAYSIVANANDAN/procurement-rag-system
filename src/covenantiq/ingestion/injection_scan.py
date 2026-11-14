"""Prompt-injection scan for document text (spec 18.7).

A contract is untrusted input to an LLM. This scores how much a passage reads as an instruction
aimed at an AI rather than contract language, and strips invisible characters that can hide such
instructions. Flagged text is still stored and retrievable (it may be legitimate); downstream
prompts wrap it with a stronger warning.
"""

import re
import unicodedata
from dataclasses import dataclass

FLAG_THRESHOLD = 0.5

# Zero-width characters, bidirectional overrides/isolates, and other invisible formatting.
_INVISIBLE = re.compile(
    r"[\u200b\u200c\u200d\u200e\u200f\u2060\u2061\u2062\u2063\u2064\ufeff"
    r"\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u00ad\u180e]"
)

_AI_DIRECTED = [
    (
        "ignore_instructions",
        0.7,
        re.compile(
            r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}"
            r"\b(previous|prior|above|earlier|preceding|all|any)\b[^.\n]{0,20}"
            r"\b(instructions?|prompts?|rules|directions|messages|context)\b",
            re.I,
        ),
    ),
    ("you_are_now", 0.5, re.compile(r"\byou are now\b|\bfrom now on,? you\b", re.I)),
    (
        "system_prompt",
        0.25,  # software contracts mention system messages; only meaningful with other evidence
        re.compile(r"\b(system|developer) (prompt|message|instructions?)\b", re.I),
    ),
    (
        "hide_from_user",
        0.6,
        re.compile(
            r"\bdo not (tell|inform|reveal|mention|show)\b[^.\n]{0,30}"
            r"\b(user|reader|customer|human)\b",
            re.I,
        ),
    ),
    (
        "respond_with",
        0.4,
        # "respond with a written notice" is contract language; dictating the output is not
        re.compile(
            r"\b(respond|reply|answer|output)\s+(only\s+)?with\s*"
            r"(:|[\"\u201c']|\b(approved|yes|no|ok|true|false|compliant|accepted)\b)",
            re.I,
        ),
    ),
    (
        "reveal_prompt",
        0.6,
        re.compile(
            r"\b(reveal|print|repeat|show)\b[^.\n]{0,25}\b(your|the) (prompt|instructions)\b", re.I
        ),
    ),
    (
        "ai_addressed",
        0.4,
        re.compile(
            r"\b(note|message|instruction)s? (to|for) (the )?(ai|assistant|model|llm|reviewer)\b",
            re.I,
        ),
    ),
]
_ROLE_TAGS = re.compile(
    r"<\s*/?\s*(system|assistant|user)\s*>|\[/?INST\]|<\|im_(start|end)\|>|^\s*(###\s*)?(system|assistant)\s*:",
    re.I | re.M,
)
_BASE64_BLOB = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")
_SECOND_PERSON = re.compile(r"\byou(r|rs|rself)?\b", re.I)

ROLE_TAG_WEIGHT = 0.6
BASE64_WEIGHT = 0.4
INVISIBLE_WEIGHT = 0.3
SECOND_PERSON_WEIGHT = 0.3
SECOND_PERSON_MIN_COUNT = 5
SECOND_PERSON_MIN_RATIO = 0.05


@dataclass(frozen=True)
class ScanResult:
    text: str  # invisible characters removed
    score: float
    flags: tuple[str, ...]

    @property
    def flagged(self) -> bool:
        return self.score >= FLAG_THRESHOLD


def strip_invisible(text: str) -> str:
    """Remove invisible characters, then any other format-category control characters except
    ordinary whitespace."""
    cleaned = _INVISIBLE.sub("", text)
    return "".join(
        ch for ch in cleaned if ch in "\n\t\r " or unicodedata.category(ch) not in {"Cf", "Cc"}
    )


def scan(text: str) -> ScanResult:
    """Score `text` in [0, 1]. Features combine as independent evidence: 1 - prod(1 - weight)."""
    weights: list[float] = []
    flags: list[str] = []

    if _INVISIBLE.search(text):
        weights.append(INVISIBLE_WEIGHT)
        flags.append("invisible_characters")
    cleaned = strip_invisible(text)

    for name, weight, pattern in _AI_DIRECTED:
        if pattern.search(cleaned):
            weights.append(weight)
            flags.append(name)
    if _ROLE_TAGS.search(cleaned):
        weights.append(ROLE_TAG_WEIGHT)
        flags.append("role_tags")
    if _BASE64_BLOB.search(cleaned):
        weights.append(BASE64_WEIGHT)
        flags.append("base64_blob")

    words = cleaned.split()
    second_person = len(_SECOND_PERSON.findall(cleaned))
    if (
        second_person >= SECOND_PERSON_MIN_COUNT
        and words
        and second_person / len(words) >= SECOND_PERSON_MIN_RATIO
    ):
        weights.append(SECOND_PERSON_WEIGHT)
        flags.append("second_person_density")

    remaining = 1.0
    for weight in weights:
        remaining *= 1.0 - weight
    return ScanResult(text=cleaned, score=round(1.0 - remaining, 3), flags=tuple(flags))
