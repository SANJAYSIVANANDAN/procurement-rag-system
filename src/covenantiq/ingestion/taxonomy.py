import re
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

# src/covenantiq/ingestion/taxonomy.py -> repository (or /app in the image) root
DEFAULT_TAXONOMY_PATH = Path(__file__).resolve().parents[3] / "config" / "clause_taxonomy.yaml"

# Spec 18.6: the heading and the first 300 characters of the clause are what get matched.
MATCH_WINDOW_CHARS = 300


class TaxonomyFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    types: dict[str, list[str]]

    @field_validator("types")
    @classmethod
    def _phrases_are_valid_and_non_empty(cls, types: dict[str, list[str]]) -> dict[str, list[str]]:
        for name, phrases in types.items():
            if not phrases:
                raise ValueError(f"clause type {name!r} has no phrases")
            for phrase in phrases:
                re.compile(phrase)  # a bad regular expression fails at load time, not mid-ingest
        return types


class Taxonomy:
    """Keyword taxonomy for clause types. Matching is deterministic and multi-label."""

    def __init__(self, definition: TaxonomyFile) -> None:
        self.version = definition.version
        self._patterns: dict[str, re.Pattern[str]] = {}
        for name, phrases in definition.types.items():
            alternatives = "|".join(
                r"\s+".join(part for part in phrase.split()) for phrase in phrases
            )
            self._patterns[name] = re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE)

    @property
    def types(self) -> list[str]:
        return list(self._patterns)

    def classify(self, heading: str | None, body: str) -> list[str]:
        """Clause types whose phrases appear in the heading or the start of the body."""
        window = f"{heading or ''}\n{body[:MATCH_WINDOW_CHARS]}"
        return [name for name, pattern in self._patterns.items() if pattern.search(window)]


def load_taxonomy(path: Path = DEFAULT_TAXONOMY_PATH) -> Taxonomy:
    with path.open(encoding="utf-8") as handle:
        return Taxonomy(TaxonomyFile.model_validate(yaml.safe_load(handle)))


@lru_cache
def get_taxonomy() -> Taxonomy:
    return load_taxonomy()
