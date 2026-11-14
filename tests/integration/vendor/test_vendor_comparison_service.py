"""Comparing vendors on real PostgreSQL: the preconditions of spec 21.8, the run, and reading it."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from covenantiq.core.enums import JobStatus
from covenantiq.core.errors import (
    AssessmentRequired,
    Conflict,
    IncompatibleAssessments,
    NotFound,
    ValidationFailed,
)
from covenantiq.services.dto import Principal
from tests.integration.analysis.stack import clean_checkpoints  # noqa: F401  (a fixture)
from tests.integration.memory.helpers import new_user
from tests.integration.vendor.stack import UowFactory, VendorStack, build_vendor_stack

pytestmark = pytest.mark.usefixtures("clean_checkpoints")


@pytest.fixture
async def stack(uow_factory: UowFactory, database_url: str) -> VendorStack:
    return await build_vendor_stack(uow_factory, database_url, documents=1)


async def two_assessed(stack: VendorStack) -> tuple[UUID, UUID]:
    """Acme (the stack's vendor) and a second vendor, both assessed."""
    other, _ = await stack.new_vendor("Globex")
    await stack.assess()
    await stack.assess(other)
    return stack.vendor_id, other


async def compare(stack: VendorStack, ids: list[UUID], **kw: bool):  # type: ignore[no-untyped-def]
    started = await stack.comparisons.compare(stack.principal, ids, **kw)
    return started, await stack.comparisons.run(started.job.id)


class TestStarting:
    async def test_it_queues_a_job_and_creates_the_comparison_row(self, stack: VendorStack) -> None:
        acme, globex = await two_assessed(stack)
        started = await stack.comparisons.compare(stack.principal, [acme, globex])

        assert started.job.status is JobStatus.QUEUED
        assert [t.value for t, j in stack.dispatcher.analysis if j == started.job.id] == [
            "vendor_comparison"
        ]
        pending = await stack.comparisons.get(stack.principal, started.comparison_id)
        assert (
            pending.job.status is JobStatus.QUEUED
            and pending.result == {}
            and pending.narrative is None
        )
        assert pending.vendor_ids == [str(acme), str(globex)] and len(pending.assessment_ids) == 2

    async def test_a_vendor_without_a_completed_assessment_is_a_409_naming_it(
        self, stack: VendorStack
    ) -> None:
        other, _ = await stack.new_vendor("Globex")
        await stack.assess()
        with pytest.raises(AssessmentRequired, match="Globex") as caught:
            await stack.comparisons.compare(stack.principal, [stack.vendor_id, other])
        assert caught.value.details["vendor_ids"] == [str(other)]

    async def test_assessments_scored_with_different_configurations_are_refused(
        self, stack: VendorStack
    ) -> None:
        acme, globex = await two_assessed(stack)
        async with stack.uow_factory() as uow:
            await uow.session.execute(
                text(
                    "UPDATE vendor_risk_assessments "
                    "SET scoring_config_version = 'vendor_scoring_v0@0.9.0' WHERE vendor_id = :v"
                ),
                {"v": globex},
            )
            await uow.commit()
        with pytest.raises(IncompatibleAssessments) as caught:
            await stack.comparisons.compare(stack.principal, [acme, globex])
        assert "vendor_scoring_v0@0.9.0" in caught.value.details["versions"]
        assert stack.dispatcher.analysis[-1][0].value != "vendor_comparison"

    async def test_an_assessment_older_than_its_documents_is_refused_unless_freshness_is_waived(
        self, stack: VendorStack
    ) -> None:
        acme, globex = await two_assessed(stack)
        async with stack.uow_factory() as uow:
            await uow.session.execute(
                text("UPDATE documents SET updated_at = :t WHERE vendor_id = :v"),
                {"t": datetime.now(UTC) + timedelta(minutes=5), "v": acme},
            )
            await uow.commit()
        with pytest.raises(Conflict, match="older than its documents") as caught:
            await stack.comparisons.compare(stack.principal, [acme, globex])
        assert caught.value.details["vendor_ids"] == [str(acme)]
        waived = await stack.comparisons.compare(
            stack.principal, [acme, globex], require_fresh=False
        )
        assert waived.job.status is JobStatus.QUEUED

    async def test_a_newer_contract_analysis_also_makes_an_assessment_stale(
        self, stack: VendorStack
    ) -> None:
        acme, globex = await two_assessed(stack)
        await stack.contract.start(stack.principal, stack.document_ids[0], force=True)
        await stack.run_pending_contract_analyses()
        with pytest.raises(Conflict):
            await stack.comparisons.compare(stack.principal, [acme, globex])

    @pytest.mark.parametrize("make", ["duplicate", "one", "six"])
    async def test_the_number_of_vendors_is_checked(self, stack: VendorStack, make: str) -> None:
        acme, _ = await two_assessed(stack)
        ids = {"duplicate": [acme, acme], "one": [acme], "six": [uuid4() for _ in range(6)]}[make]
        with pytest.raises(ValidationFailed):
            await stack.comparisons.compare(stack.principal, ids)

    async def test_an_unknown_or_other_users_vendor_is_not_found(
        self, stack: VendorStack, uow_factory: UowFactory
    ) -> None:
        acme, globex = await two_assessed(stack)
        with pytest.raises(NotFound):
            await stack.comparisons.compare(stack.principal, [acme, uuid4()])
        other = Principal(user_id=(await new_user(uow_factory)).owner_id, email="o@example.com")
        with pytest.raises(NotFound):
            await stack.comparisons.compare(other, [acme, globex])


class TestRunning:
    async def test_the_matrix_and_the_summary_are_stored(self, stack: VendorStack) -> None:
        acme, globex = await two_assessed(stack)
        started, result = await compare(stack, [acme, globex])

        assert result["status"] == "succeeded" and result["narrative"] == "model"
        dto = await stack.comparisons.get(stack.principal, started.comparison_id)
        assert dto.job.status is JobStatus.SUCCEEDED
        assert dto.result["scoring_config_version"] == "vendor_scoring_v1@1.0.0"
        assert {v["name"] for v in dto.result["vendors"]} == {"Acme", "Globex"}
        assert len(dto.result["dimensions"]) == 6 and dto.result["signals"]
        assert dto.narrative and dto.result["narrative_source"] == "model"
        assert len(dto.result["ranking"]) == 2  # both complete and not provisional

    async def test_signals_one_vendor_lacks_evidence_for_are_not_compared(
        self, stack: VendorStack
    ) -> None:
        acme, globex = await two_assessed(stack)
        async with stack.uow_factory() as uow:  # Globex's SLA signal becomes a plain absence
            await uow.session.execute(
                text(
                    "UPDATE vendor_risk_dimensions d SET signals = ("
                    "SELECT jsonb_agg(CASE WHEN s->>'signal_id' = 'OPS-SLA' "
                    "THEN s || jsonb_build_object('status', 'absent', "
                    "'gap', 'absent_in_all_documents', 'risk', null) "
                    "ELSE s END) FROM jsonb_array_elements(d.signals) s) "
                    "FROM vendor_risk_assessments a WHERE d.assessment_id = a.id "
                    "AND a.vendor_id = :v AND d.dimension = 'operational'"
                ),
                {"v": globex},
            )
            await uow.commit()
        started, _ = await compare(stack, [acme, globex])
        dto = await stack.comparisons.get(stack.principal, started.comparison_id)
        row = next(s for s in dto.result["signals"] if s["signal_id"] == "OPS-SLA")
        assert not row["comparable"] and row["reason"] == "Missing in Globex — cannot compare"

    async def test_a_second_delivery_of_a_finished_comparison_is_skipped(
        self, stack: VendorStack
    ) -> None:
        acme, globex = await two_assessed(stack)
        started, _ = await compare(stack, [acme, globex])
        assert await stack.comparisons.run(started.job.id) == {"status": "skipped"}

    async def test_a_comparison_of_three_vendors(self, stack: VendorStack) -> None:
        acme, globex = await two_assessed(stack)
        initech, _ = await stack.new_vendor("Initech")
        await stack.assess(initech)
        started, _ = await compare(stack, [acme, globex, initech])
        dto = await stack.comparisons.get(stack.principal, started.comparison_id)
        assert len(dto.result["vendors"]) == 3 and len(dto.result["ranking"]) == 3


class TestReading:
    async def test_another_user_cannot_read_a_comparison(
        self, stack: VendorStack, uow_factory: UowFactory
    ) -> None:
        acme, globex = await two_assessed(stack)
        started, _ = await compare(stack, [acme, globex])
        other = Principal(user_id=(await new_user(uow_factory)).owner_id, email="o@example.com")
        with pytest.raises(NotFound):
            await stack.comparisons.get(other, started.comparison_id)
        with pytest.raises(NotFound):
            await stack.comparisons.get(stack.principal, uuid4())
