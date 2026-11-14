"""The vendor comparison graph's nodes (spec 22.7). The matrix is computed by pure code; the model
only writes a summary of it, and a summary that does not check out is replaced by a template."""

import json
from typing import Any
from uuid import UUID

from langgraph.config import get_stream_writer

from covenantiq.ai.llm.base import ChatMessage
from covenantiq.ai.prompts import load_prompt
from covenantiq.ai.schemas import ComparisonNarrative
from covenantiq.analysis.vendor.comparison import (
    compare,
    template_summary,
    validate_narrative,
)
from covenantiq.core.errors import CovenantError
from covenantiq.core.logging import get_logger
from covenantiq.graphs.contract_analysis.nodes import counts
from covenantiq.graphs.errors import graph_error
from covenantiq.graphs.vendor_comparison.deps import ComparisonDeps, LoadedAssessment
from covenantiq.graphs.vendor_comparison.state import ComparisonState

log = get_logger(__name__)

ASSESSMENT_REQUIRED = "ASSESSMENT_REQUIRED"
INCOMPATIBLE = "INCOMPATIBLE_ASSESSMENTS"


def _error(code: str, message: str, node: str) -> dict[str, Any]:
    return {"code": code, "message": message, "retryable": False, "node": node}


class ComparisonNodes:
    def __init__(self, deps: ComparisonDeps) -> None:
        self._d = deps

    async def load_latest_assessments(self, state: ComparisonState) -> dict[str, Any]:
        get_stream_writer()({"event": "progress", "stage": "loading", "fraction": 0.1})
        wanted = [UUID(v) for v in state["vendor_ids"]]
        loaded = await self._d.store.load_assessments(UUID(state["owner_id"]), wanted)
        have = {item.snapshot.vendor_id for item in loaded}
        if have != set(wanted):
            return {
                "error": _error(
                    ASSESSMENT_REQUIRED,
                    f"{len(set(wanted) - have)} vendor(s) have no completed assessment.",
                    "load_latest_assessments",
                )
            }
        return {"loaded": [item.model_dump(mode="json") for item in loaded]}

    async def check_config_compatibility(self, state: ComparisonState) -> dict[str, Any]:
        """Assessments scored with different configurations are not comparable (spec 21.8)."""
        loaded = [LoadedAssessment.model_validate(item) for item in state["loaded"]]
        versions = {item.scoring_config_version for item in loaded}
        current = self._d.config.scoring.version
        if len(versions) > 1 or versions != {current}:
            return {
                "error": _error(
                    INCOMPATIBLE,
                    "The assessments were scored with different configurations "
                    f"({', '.join(sorted(versions))}; current {current}). Re-run them.",
                    "check_config_compatibility",
                )
            }
        return {}

    async def compute_comparability(self, state: ComparisonState) -> dict[str, Any]:
        get_stream_writer()({"event": "progress", "stage": "comparing", "fraction": 0.4})
        loaded = [LoadedAssessment.model_validate(item) for item in state["loaded"]]
        result = compare(self._d.config.scoring, [item.snapshot for item in loaded])
        return {"result": result}

    async def generate_narrative(self, state: ComparisonState) -> dict[str, Any]:
        get_stream_writer()({"event": "progress", "stage": "writing_summary", "fraction": 0.6})
        loaded = [LoadedAssessment.model_validate(item) for item in state["loaded"]]
        quotes = {
            item.snapshot.vendor_name: {
                dimension: texts[:3] for dimension, texts in item.quotes.items() if texts
            }
            for item in loaded
        }
        prompt = (
            "<matrix>\n"
            + json.dumps(state["result"], indent=1, default=str)
            + "\n</matrix>\n<evidence_quotes>\n"
            + json.dumps(quotes, indent=1)
            + "\n</evidence_quotes>"
        )
        request = self._d.routes.request(
            "narrative", load_prompt("comparison_narrative.v1"), [ChatMessage("user", prompt)]
        )
        try:
            narrative = await self._d.llm.structured(request, ComparisonNarrative)
        except (CovenantError, ValueError) as exc:
            log.warning("comparison.narrative_failed", error=type(exc).__name__)
            return {
                "draft": None,
                "notes": [f"narrative unavailable ({graph_error(exc, 'narrative').code})"],
            }
        return {"draft": narrative.value.summary, "usage": counts(narrative.usage)}

    async def validate_narrative_references(self, state: ComparisonState) -> dict[str, Any]:
        draft = state.get("draft")
        result = state["result"]
        if draft:
            known = {s.id for s in self._d.config.signals.signals}
            problems = validate_narrative(draft, result, known)
            if not problems:
                return {"narrative": draft.strip(), "narrative_source": "model"}
            return {
                "narrative": template_summary(result),
                "narrative_source": "template",
                "notes": [
                    "the model's summary was replaced by the template: " + "; ".join(problems)
                ],
            }
        return {"narrative": template_summary(result), "narrative_source": "template"}

    async def persist_comparison(self, state: ComparisonState) -> dict[str, Any]:
        get_stream_writer()({"event": "progress", "stage": "saving", "fraction": 0.9})
        result = {
            **state["result"],
            "narrative_source": state["narrative_source"],
            "notes": state.get("notes", []),
        }
        await self._d.store.persist_comparison(
            UUID(state["owner_id"]),
            UUID(state["job_id"]),
            UUID(state["comparison_id"]),
            result,
            state["narrative"],
        )
        get_stream_writer()({"event": "progress", "stage": "done", "fraction": 1.0})
        return {}
