"""Retrieval tuning, loaded from `config/rag.yaml` (spec 19, 35)."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field

from covenantiq.core.config import Settings

DEFAULT_RAG_CONFIG_PATH = Path("config/rag.yaml")

Positive = Annotated[int, Field(gt=0)]
Fraction = Annotated[float, Field(ge=0.0, le=1.0)]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RetrievalConfig(_Section):
    k_dense: Positive = 30
    k_lexical: Positive = 30
    k_final: Positive = 8
    rerank_candidates: Positive = 30
    rrf_k: Positive = 60
    weight_dense: Annotated[float, Field(ge=0)] = 1.0
    weight_lexical: Annotated[float, Field(ge=0)] = 1.0
    clause_type_boost: Annotated[float, Field(ge=1.0)] = 1.1
    ef_search: Annotated[int, Field(ge=1, le=1000)] = 100
    rerank_timeout_seconds: Annotated[float, Field(gt=0)] = 3.0
    rerank_doc_max_chars: Positive = 4000
    max_keywords: Positive = 12
    query_embedding_cache_seconds: Positive = 86400
    rerank_cache_seconds: Positive = 3600


class ContextConfig(_Section):
    max_tokens: Positive = 6000
    parent_max_tokens: Positive = 1500


class EvidenceConfig(_Section):
    rerank_strong: Fraction = 0.55
    rerank_moderate: Fraction = 0.35
    rerank_single_strong: Fraction = 0.70
    min_supporting_chunks: Positive = 2
    keyword_coverage: Fraction = 0.5
    rrf_agreement_rank: Positive = 10


class CitationConfig(_Section):
    quote_fuzzy_ratio: Annotated[int, Field(ge=0, le=100)] = 90
    overlap_threshold: Fraction = 0.35
    partial_unsupported_fraction: Fraction = 0.30
    quote_max_chars: Positive = 300


class AnalysisConfig(_Section):
    """Contract analysis tuning (spec 20.4, 20.5, 22.5)."""

    absent_threshold: Fraction = 0.30  # best rerank score below this supports "absent"
    pages_with_text_ratio: Fraction = 0.95  # extraction quality needed to claim absence
    rules_per_batch: Positive = 4
    k_final_per_query: Positive = 6
    max_sources_per_rule: Positive = 8
    context_tokens_per_rule: Positive = 3000
    max_degraded_fraction: Fraction = 0.5  # more rules than this failing fails the job
    max_contradiction_pairs: Positive = 5
    obligation_sources: Positive = 10


class RagConfig(_Section):
    version: int = 1
    retrieval: RetrievalConfig = RetrievalConfig()
    context: ContextConfig = ContextConfig()
    evidence: EvidenceConfig = EvidenceConfig()
    citations: CitationConfig = CitationConfig()
    analysis: AnalysisConfig = AnalysisConfig()

    def with_settings(self, settings: Settings) -> "RagConfig":
        """Environment variables that already exist win over the file."""
        return self.model_copy(
            update={
                "context": ContextConfig(
                    max_tokens=settings.context_max_tokens,
                    parent_max_tokens=settings.parent_max_tokens,
                )
            }
        )


def load_rag_config(path: Path | None = None) -> RagConfig:
    """The config file, or the defaults if it does not exist. A malformed file is an error: a
    silently ignored threshold is worse than a refusal to start."""
    target = path or DEFAULT_RAG_CONFIG_PATH
    if not target.is_file():
        return RagConfig()
    with target.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return RagConfig.model_validate(raw)


@lru_cache(maxsize=1)
def get_rag_config(path: Path | None = None) -> RagConfig:
    return load_rag_config(path)
