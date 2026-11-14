"""Is the retrieved evidence enough to answer from? (spec 19.10)

The gate decides between three outcomes: answer confidently (strong), answer but say what the
documents do not cover (moderate), or abstain with a template and no model call (weak, none). The
thresholds are initial values in `config/rag.yaml`, to be calibrated against the evaluation set.
"""

import re
from collections.abc import Collection, Sequence
from uuid import UUID

import snowballstemmer

from covenantiq.rag.config import EvidenceConfig
from covenantiq.rag.query_analysis import STOP_WORDS
from covenantiq.rag.types import ContextBundle, EvidenceVerdict, RetrievalResult

LEVELS = ("none", "weak", "moderate", "strong")  # worst to best

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-']*")
_stemmer = snowballstemmer.stemmer("english")


def stems(text: str) -> set[str]:
    """The distinct stemmed content words of a text (stop words dropped)."""
    words = [w.lower().strip("-'") for w in _WORD.findall(text)]
    return set(_stemmer.stemWords([w for w in words if w and w not in STOP_WORDS]))


def _downgrade(level: str) -> str:
    return LEVELS[max(1, LEVELS.index(level) - 1)] if level != "none" else "none"


class EvidenceAssessor:
    def __init__(self, config: EvidenceConfig | None = None) -> None:
        self._config = config or EvidenceConfig()

    def assess(
        self,
        keywords: Sequence[str],
        result: RetrievalResult,
        context: ContextBundle,
        *,
        required_documents: Collection[UUID] = (),
    ) -> EvidenceVerdict:
        """`keywords` are the query's key terms; `required_documents` is for comparison questions,
        where every document named must contribute something."""
        config = self._config
        reasons: list[str] = []
        signals: dict[str, object] = {"rerank_skipped": result.trace.rerank_skipped}
        if not result.chunks or not context.sources:
            return EvidenceVerdict(False, "none", ["no passage was retrieved"], signals)

        if result.trace.rerank_skipped:
            level = self._by_rank_agreement(result, reasons, signals)
        else:
            level = self._by_rerank_score(result, reasons, signals)

        coverage = self._keyword_coverage(keywords, context)
        signals["keyword_coverage"] = coverage
        if coverage is not None and coverage < config.keyword_coverage:
            level = _downgrade(level)
            reasons.append(
                f"only {coverage:.0%} of the key terms appear in the retrieved text "
                f"(at least {config.keyword_coverage:.0%} needed)"
            )

        missing = self._uncovered_documents(required_documents, result)
        if missing:
            level = _downgrade(level)
            signals["uncovered_documents"] = [str(d) for d in missing]
            reasons.append(f"{len(missing)} of the compared documents have no relevant passage")

        return EvidenceVerdict(level in ("strong", "moderate"), level, reasons, signals)

    def _by_rerank_score(
        self, result: RetrievalResult, reasons: list[str], signals: dict[str, object]
    ) -> str:
        config = self._config
        scores = sorted((c.rerank_score or 0.0 for c in result.chunks), reverse=True)
        top = scores[0]
        supporting = sum(1 for score in scores if score >= config.rerank_moderate)
        signals.update(top_score=top, supporting_chunks=supporting)
        if top >= config.rerank_strong:
            if supporting >= config.min_supporting_chunks or top >= config.rerank_single_strong:
                return "strong"
            reasons.append("the best passage is relevant but nothing else supports it")
            return "moderate"
        if top >= config.rerank_moderate:
            reasons.append(f"the best passage scores {top:.2f}, below {config.rerank_strong}")
            return "moderate"
        reasons.append(f"the best passage scores only {top:.2f}")
        return "weak"

    def _by_rank_agreement(
        self, result: RetrievalResult, reasons: list[str], signals: dict[str, object]
    ) -> str:
        """No reranker scores: the top result must be in both top-N lists to count as moderate,
        and nothing is ever strong (spec 19.8, 19.10)."""
        limit = self._config.rrf_agreement_rank
        top = result.chunks[0]
        agree = (
            top.dense_rank is not None
            and top.lexical_rank is not None
            and top.dense_rank <= limit
            and top.lexical_rank <= limit
        )
        signals["rank_agreement"] = agree
        reasons.append("reranking was skipped, so only the search ranks were available")
        if agree:
            return "moderate"
        reasons.append(f"the best passage is not in the top {limit} of both searches")
        return "weak"

    @staticmethod
    def _keyword_coverage(keywords: Sequence[str], context: ContextBundle) -> float | None:
        wanted = {stem for keyword in keywords for stem in stems(keyword)}
        if not wanted:
            return None
        present = stems(" ".join(source.text for source in context.sources))
        return len(wanted & present) / len(wanted)

    def _uncovered_documents(
        self, required: Collection[UUID], result: RetrievalResult
    ) -> list[UUID]:
        floor = self._config.rerank_moderate
        covered = {
            c.document_id
            for c in result.chunks
            if result.trace.rerank_skipped or (c.rerank_score or 0.0) >= floor
        }
        return [document for document in required if document not in covered]
