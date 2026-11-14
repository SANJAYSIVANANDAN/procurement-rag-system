"""The vendor comparison graph on fakes: preconditions, the matrix, and the narrative fallback."""

from collections.abc import Sequence
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from covenantiq.ai.llm.base import LLMUnavailable
from covenantiq.ai.llm.fake import FakeLLMProvider
from covenantiq.ai.llm.routes import RouteTable
from covenantiq.ai.schemas import ComparisonNarrative
from covenantiq.core.errors import PermanentTaskError
from covenantiq.graphs.vendor_comparison.deps import ComparisonDeps, LoadedAssessment
from covenantiq.graphs.vendor_comparison.runner import VendorComparisonRunner
from tests.unit.analysis import test_vendor_comparison as vc
from tests.unit.analysis.test_vendor_comparison import (
    ACME,
    CONFIG,
    GLOBEX,
    INITECH,
    everything,
    snapshot,
)

GOOD = vc.TestNarrative.GOOD
SPARSE = {k: v for k, v in everything("favorable").items() if k.startswith(("LEG-", "DS-"))}


class FakeComparisonStore:
    def __init__(self, loaded: list[LoadedAssessment]) -> None:
        self.loaded = loaded
        self.persisted: dict[UUID, tuple[dict[str, Any], str]] = {}
        self.persist_calls = 0
        self.progress: list[tuple[str, float]] = []

    async def load_assessments(
        self, owner_id: UUID, vendor_ids: Sequence[UUID]
    ) -> list[LoadedAssessment]:
        return [item for item in self.loaded if item.snapshot.vendor_id in set(vendor_ids)]

    async def persist_comparison(self, owner_id, job_id, comparison_id, result, narrative) -> None:  # type: ignore[no-untyped-def]
        self.persist_calls += 1
        self.persisted[comparison_id] = (result, narrative)

    async def report(self, job_id: UUID, stage: str, fraction: float) -> None:
        self.progress.append((stage, fraction))


def loaded(
    vendor: UUID, name: str, statuses: dict[str, str], version: str = "vendor_scoring_v1@1.0.0"
) -> LoadedAssessment:
    return LoadedAssessment(
        snapshot=snapshot(vendor, name, statuses),
        scoring_config_version=version,
        quotes={"legal": ["The Supplier's liability is capped at twelve months of fees."]},
    )


class Comparison:
    def __init__(self, items: list[LoadedAssessment]) -> None:
        self.store = FakeComparisonStore(items)
        self.llm = FakeLLMProvider()
        self.saver = InMemorySaver()
        self.comparison_id = uuid4()
        self.vendor_ids = [item.snapshot.vendor_id for item in items]
        self.llm.script("narrative", lambda r: ComparisonNarrative(summary=GOOD))
        saver = self.saver

        @asynccontextmanager
        async def factory():  # type: ignore[no-untyped-def]
            yield saver

        self.runner = VendorComparisonRunner(
            ComparisonDeps(
                llm=self.llm, routes=RouteTable({}, "m"), store=self.store, config=CONFIG
            ),
            self.store,
            factory,
            progress_interval=0.0,
        )

    async def run(self, vendor_ids: list[UUID] | None = None) -> Any:
        return await self.runner.run(
            job_id=uuid4(),
            owner_id=uuid4(),
            comparison_id=self.comparison_id,
            vendor_ids=vendor_ids or self.vendor_ids,
        )

    @property
    def result(self) -> dict[str, Any]:
        return self.store.persisted[self.comparison_id][0]

    @property
    def narrative(self) -> str:
        return self.store.persisted[self.comparison_id][1]


def pair(second: dict[str, str] | None = None) -> Comparison:
    return Comparison(
        [loaded(ACME, "Acme", everything("favorable")), loaded(GLOBEX, "Globex", second or SPARSE)]
    )


class TestARun:
    async def test_the_matrix_is_stored_with_the_config_version_and_progress_ends_at_one(
        self,
    ) -> None:
        c = pair()
        await c.run()
        assert c.result["scoring_config_version"] == "vendor_scoring_v1@1.0.0"
        assert [v["name"] for v in c.result["vendors"]] == ["Acme", "Globex"]
        assert c.store.persist_calls == 1 and c.store.progress[-1][1] == 1.0

    async def test_a_comparison_with_a_provisional_vendor_has_no_ranking_and_says_why(self) -> None:
        c = pair()
        await c.run()
        assert c.result["ranking"] == [] and "Globex" in c.result["ranking_suppressed_reason"]

    async def test_complete_vendors_are_ranked(self) -> None:
        c = pair(everything("ambiguous"))
        await c.run()
        assert c.result["ranking"][0] == str(ACME)

    async def test_the_checkpoints_are_removed_afterwards(self) -> None:
        c = pair()
        await c.run()
        assert await c.saver.aget_tuple({"configurable": {"thread_id": "x"}}) is None


class TestNarrative:
    async def test_a_sound_model_summary_is_used(self) -> None:
        c = pair()
        await c.run()
        assert c.narrative == GOOD.strip() and c.result["narrative_source"] == "model"

    async def test_the_model_receives_the_matrix_and_evidence_quotes(self) -> None:
        c = pair()
        await c.run()
        sent = c.llm.requests[0].messages[0].content
        assert "<matrix>" in sent and '"scoring_config_version"' in sent
        assert "capped at twelve months of fees" in sent

    async def test_a_summary_that_compares_what_is_not_comparable_is_replaced_by_the_template(
        self,
    ) -> None:
        c = pair()
        bad = GOOD + " Acme is clearly better than Globex on compliance."
        c.llm.script("narrative", lambda r: ComparisonNarrative(summary=bad))
        await c.run()
        assert c.result["narrative_source"] == "template"
        assert "compliance: not comparable" in c.narrative
        assert any("not comparable" in n for n in c.result["notes"])

    async def test_an_invented_signal_id_gets_the_template(self) -> None:
        c = pair()
        c.llm.script(
            "narrative",
            lambda r: ComparisonNarrative(summary=GOOD + " XYZ-MAGIC differs."),
        )
        await c.run()
        assert c.result["narrative_source"] == "template"

    async def test_an_unavailable_model_never_fails_the_job(self) -> None:
        c = pair()
        c.llm.script("narrative", lambda r: (_ for _ in ()).throw(LLMUnavailable("down")))
        await c.run()
        assert c.result["narrative_source"] == "template" and c.narrative
        assert any("narrative unavailable" in n for n in c.result["notes"])

    async def test_the_template_is_always_deterministic(self) -> None:
        a, b = pair(), pair()
        for c in (a, b):
            c.llm.script("narrative", lambda r: (_ for _ in ()).throw(LLMUnavailable("down")))
            await c.run()
        assert a.narrative == b.narrative


class TestPreconditionsInTheGraph:
    async def test_a_vendor_without_an_assessment_stops_the_run(self) -> None:
        c = pair()
        with pytest.raises(PermanentTaskError) as caught:
            await c.run([ACME, GLOBEX, INITECH])
        assert caught.value.job_code == "ASSESSMENT_REQUIRED" and c.store.persist_calls == 0
        assert c.llm.calls == 0

    async def test_assessments_scored_with_different_configurations_are_refused(self) -> None:
        c = Comparison(
            [
                loaded(ACME, "Acme", everything("favorable")),
                loaded(
                    GLOBEX, "Globex", everything("favorable"), version="vendor_scoring_v0@0.9.0"
                ),
            ]
        )
        with pytest.raises(PermanentTaskError) as caught:
            await c.run()
        assert caught.value.job_code == "INCOMPATIBLE_ASSESSMENTS" and c.llm.calls == 0

    async def test_an_assessment_from_an_older_configuration_than_the_current_one_is_refused(
        self,
    ) -> None:
        c = Comparison(
            [
                loaded(ACME, "Acme", everything("favorable"), version="vendor_scoring_v0@0.9.0"),
                loaded(
                    GLOBEX, "Globex", everything("favorable"), version="vendor_scoring_v0@0.9.0"
                ),
            ]
        )
        with pytest.raises(PermanentTaskError) as caught:
            await c.run()
        assert caught.value.job_code == "INCOMPATIBLE_ASSESSMENTS"
