"""Keywords for the full-text half of hybrid search (spec 19.2 step 2).

Rule-based: no LLM call. A query is turned into a PostgreSQL `websearch_to_tsquery` string made of
its content words joined with `or`, plus any quoted phrases kept intact. Legal text needs exact
terms ("indemnify", "Schedule 3", a defined term), which is why this half exists next to the
vector search.
"""

import re
from dataclasses import dataclass

# Words that carry no search value. "shall", "may", "must" and friends are here on purpose: in a
# contract they are on nearly every line, so as search terms they match everything.
STOP_WORDS = frozenset(
    """
    a about above after again against all also am an and any are as at be because been before being
    below between both but by can could did do does doing down during each few for from further had
    has have having he her here hers him his how i if in into is it its itself just me more most my
    no nor not now of off on once only or other our ours out over own same she should so some such
    than that the their theirs them then there these they this those through to too under until up
    very was we were what when where which while who whom whose why will with would you your yours
    shall may must might
    contract contracts agreement agreements document documents clause clauses section sections
    say says said tell explain describe please give show list summarize summarise state states
    mention mentioned regarding concerning relating related provide provided
    """.split()
)

_QUOTED = re.compile(r"[\"“]([^\"”]{2,200})[\"”]")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-/']*")


@dataclass(frozen=True)
class KeywordExtraction:
    keywords: list[str]
    phrases: list[str]
    fts_text: str  # for websearch_to_tsquery; empty when there is nothing to search for


def _clean(token: str) -> str:
    return token.strip(".-/'").lower()


def extract_keywords(query: str, *, max_keywords: int = 12) -> KeywordExtraction:
    """Content words (stop words and duplicates removed, order kept) and quoted phrases."""
    phrases: list[str] = []

    def take_phrase(match: re.Match[str]) -> str:
        phrase = " ".join(match.group(1).split())
        if len(phrase.split()) < 2:
            return f" {phrase} "  # one quoted word is just a keyword, quotes or not
        if phrase.lower() not in {existing.lower() for existing in phrases}:
            phrases.append(phrase)
        return " "

    remainder = _QUOTED.sub(take_phrase, query)

    keywords: list[str] = []
    for raw in _TOKEN.findall(remainder):
        token = _clean(raw)
        if not token or token in STOP_WORDS or token in keywords:
            continue
        if len(token) < 2 and not token.isdigit():
            continue
        keywords.append(token)
        if len(keywords) >= max_keywords:
            break
    return KeywordExtraction(keywords, phrases, build_fts_text(keywords, phrases))


def build_fts_text(keywords: list[str], phrases: list[str]) -> str:
    """`"limitation of liability" or cap or consequential`. Quoted phrases stay phrases; every
    other term is alternative evidence. Nothing here is ever concatenated into SQL: the string is
    passed as a bound parameter, and `websearch_to_tsquery` never raises on odd syntax."""
    parts = [f'"{phrase.replace(chr(34), "")}"' for phrase in phrases]
    parts.extend(keywords)
    return " or ".join(parts)
