"""Obligations and deadlines (spec 20.6): what the model extracted, checked against the sources."""

import re
from collections.abc import Sequence

from covenantiq.ai.schemas import ObligationItem
from covenantiq.analysis.contract.types import ObligationRecord, SourceRef

QUERIES = (
    "shall notify within days written notice no later than",
    "days prior to expiry renewal notice termination notice period",
    "must provide deliver submit by deadline within days of",
)
MAX_FIELD_CHARS = 400
_NUMBER = re.compile(r"\d+")


def _clip(value: str | None, limit: int = MAX_FIELD_CHARS) -> str | None:
    if value is None:
        return None
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def validate_obligations(
    items: Sequence[ObligationItem], sources: Sequence[SourceRef]
) -> list[ObligationRecord]:
    """Keep an obligation only if it cites a source it was given. A deadline in days is kept only
    if that number appears in the cited text: a model that invents a number loses the number, not
    the obligation."""
    by_marker = {s.marker: s for s in sources}
    seen: set[tuple[str, str]] = set()
    records: list[ObligationRecord] = []
    for item in items:
        source = by_marker.get(item.marker)
        action = _clip(item.action)
        party = _clip(item.party, 60)
        if source is None or not action or not party:
            continue
        key = (party.lower(), action.lower())
        if key in seen:
            continue
        seen.add(key)
        days = item.deadline_days
        if days is not None and str(days) not in _NUMBER.findall(source.text):
            days = None
        records.append(
            ObligationRecord(
                party=party,
                action=action,
                deadline_text=_clip(item.deadline_text),
                deadline_days=days,
                trigger=_clip(item.trigger),
                clause_ref=item.clause_ref or source.clause_ref,
                page_number=source.page_start,
                source=source,
            )
        )
    return records
