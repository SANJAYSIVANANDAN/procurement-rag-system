import asyncio
from collections.abc import Callable

from covenantiq.core.enums import AnalysisJobType, JobStatus
from covenantiq.db.scope import OwnershipScope
from covenantiq.db.uow import UnitOfWork
from tests.integration.factories import (
    make_analysis_job,
    make_assessment,
    make_citation,
    make_conversation,
    make_document,
    make_finding,
    make_message_pair,
    make_user,
    make_vendor,
)

UowFactory = Callable[[], UnitOfWork]


async def two_scopes(uow_factory: UowFactory) -> tuple[OwnershipScope, OwnershipScope]:
    async with uow_factory() as uow:
        alice = await uow.users.add(make_user())
        bob = await uow.users.add(make_user())
        await uow.commit()
    return OwnershipScope(alice.id), OwnershipScope(bob.id)


async def test_create_or_get_active_is_idempotent(uow_factory: UowFactory) -> None:
    alice, bob = await two_scopes(uow_factory)
    async with uow_factory() as uow:
        document = await uow.documents.add(alice, make_document(alice.owner_id))
        await uow.commit()

    key = "contract_analysis:doc:v1:nonce"
    async with uow_factory() as uow:
        first, created = await uow.analysis_jobs.create_or_get_active(
            alice, make_analysis_job(alice.owner_id, document_id=document.id, idempotency_key=key)
        )
        await uow.commit()
    assert created

    async with uow_factory() as uow:
        again, created_again = await uow.analysis_jobs.create_or_get_active(
            alice, make_analysis_job(alice.owner_id, document_id=document.id, idempotency_key=key)
        )
    assert not created_again
    assert again.id == first.id

    async with uow_factory() as uow:
        assert await uow.analysis_jobs.get(bob, first.id) is None
        assert (await uow.analysis_jobs.get(alice, first.id)) is not None


async def test_concurrent_identical_requests_create_one_job(uow_factory: UowFactory) -> None:
    alice, _ = await two_scopes(uow_factory)
    async with uow_factory() as uow:
        document = await uow.documents.add(alice, make_document(alice.owner_id))
        await uow.commit()

    async def request() -> tuple[str, bool]:
        async with uow_factory() as uow:
            job, created = await uow.analysis_jobs.create_or_get_active(
                alice,
                make_analysis_job(alice.owner_id, document_id=document.id, idempotency_key="same"),
            )
            await uow.commit()
            return str(job.id), created

    results = await asyncio.gather(*(request() for _ in range(6)))
    assert len({job_id for job_id, _ in results}) == 1
    assert [created for _, created in results].count(True) == 1


async def test_job_transitions_use_cas(uow_factory: UowFactory) -> None:
    alice, _ = await two_scopes(uow_factory)
    async with uow_factory() as uow:
        document = await uow.documents.add(alice, make_document(alice.owner_id))
        job, _ = await uow.analysis_jobs.create_or_get_active(
            alice, make_analysis_job(alice.owner_id, document_id=document.id)
        )
        await uow.commit()
    async with uow_factory() as uow:
        assert not await uow.analysis_jobs.system_transition(
            job.id, [JobStatus.RUNNING], JobStatus.SUCCEEDED
        )
        assert await uow.analysis_jobs.system_transition(
            job.id, [JobStatus.QUEUED], JobStatus.RUNNING, attempt=1
        )
        await uow.commit()
    async with uow_factory() as uow:
        stored = await uow.analysis_jobs.system_get(job.id)
        assert stored is not None
        assert (stored.status, stored.attempt) == (JobStatus.RUNNING, 1)


async def test_latest_findings_come_from_the_newest_succeeded_job(uow_factory: UowFactory) -> None:
    alice, bob = await two_scopes(uow_factory)
    async with uow_factory() as uow:
        document = await uow.documents.add(alice, make_document(alice.owner_id))
        old_job, _ = await uow.analysis_jobs.create_or_get_active(
            alice, make_analysis_job(alice.owner_id, document_id=document.id)
        )
        await uow.findings.bulk_add(
            alice, [make_finding(alice.owner_id, old_job.id, document.id, "old_rule")]
        )
        await uow.analysis_jobs.system_transition(
            old_job.id, [JobStatus.QUEUED], JobStatus.SUCCEEDED
        )
        await uow.commit()

    async with uow_factory() as uow:
        new_job, _ = await uow.analysis_jobs.create_or_get_active(
            alice, make_analysis_job(alice.owner_id, document_id=document.id)
        )
        await uow.findings.bulk_add(
            alice,
            [
                make_finding(alice.owner_id, new_job.id, document.id, "new_rule", 0),
                make_finding(alice.owner_id, new_job.id, document.id, "new_rule", 1),
            ],
        )
        await uow.commit()

    async with uow_factory() as uow:
        # the new job is still queued, so the old run's findings remain the default view
        assert [f.rule_id for f in await uow.findings.latest_for_document(alice, document.id)] == [
            "old_rule"
        ]
        await uow.analysis_jobs.system_transition(
            new_job.id, [JobStatus.QUEUED], JobStatus.SUCCEEDED
        )
        await uow.commit()

    async with uow_factory() as uow:
        latest = await uow.findings.latest_for_document(alice, document.id)
        assert [(f.rule_id, f.ordinal) for f in latest] == [("new_rule", 0), ("new_rule", 1)]
        assert len(await uow.findings.for_job(alice, old_job.id)) == 1
        assert await uow.findings.latest_for_document(bob, document.id) == []
        assert await uow.findings.for_job(bob, new_job.id) == []


async def test_new_assessment_demotes_the_previous_latest(uow_factory: UowFactory) -> None:
    alice, bob = await two_scopes(uow_factory)
    async with uow_factory() as uow:
        vendor = await uow.vendors.add(alice, make_vendor(alice.owner_id))
        await uow.commit()

    async def assess() -> None:
        async with uow_factory() as uow:
            job, _ = await uow.analysis_jobs.create_or_get_active(
                alice,
                make_analysis_job(
                    alice.owner_id,
                    vendor_id=vendor.id,
                    job_type=AnalysisJobType.VENDOR_ASSESSMENT,
                ),
            )
            assessment, dimensions = make_assessment(alice.owner_id, vendor.id, job.id)
            await uow.assessments.add_assessment(alice, assessment, dimensions)
            await uow.commit()

    await assess()
    await assess()

    async with uow_factory() as uow:
        history = await uow.assessments.list(alice, vendor.id)
        assert [a.is_latest for a in history] == [True, False]
        latest = await uow.assessments.latest(alice, vendor.id)
        assert latest is not None
        assert latest.id == history[0].id
        assert {d.dimension.value for d in latest.dimensions} == {"legal", "dependency"}
        assert await uow.assessments.latest(bob, vendor.id) is None
        assert await uow.assessments.list(bob, vendor.id) == []


async def test_citations_are_readable_only_by_the_parent_owner(uow_factory: UowFactory) -> None:
    alice, bob = await two_scopes(uow_factory)
    async with uow_factory() as uow:
        document = await uow.documents.add(alice, make_document(alice.owner_id))
        conversation = await uow.conversations.create(alice, make_conversation(alice.owner_id))
        locked = await uow.conversations.get_for_update(alice, conversation.id)
        assert locked is not None
        _, reply = await uow.messages.add_pair(alice, locked, *make_message_pair())
        job, _ = await uow.analysis_jobs.create_or_get_active(
            alice, make_analysis_job(alice.owner_id, document_id=document.id)
        )
        finding = make_finding(alice.owner_id, job.id, document.id)
        await uow.findings.bulk_add(alice, [finding])
        vendor = await uow.vendors.add(alice, make_vendor(alice.owner_id))
        vendor_job, _ = await uow.analysis_jobs.create_or_get_active(
            alice,
            make_analysis_job(
                alice.owner_id, vendor_id=vendor.id, job_type=AnalysisJobType.VENDOR_ASSESSMENT
            ),
        )
        assessment, dimensions = make_assessment(alice.owner_id, vendor.id, vendor_job.id)
        await uow.assessments.add_assessment(alice, assessment, dimensions)
        await uow.citations.bulk_add(
            alice,
            [
                make_citation(document.id, message_id=reply.id),
                make_citation(document.id, finding_id=finding.id),
                make_citation(document.id, risk_dimension_id=dimensions[0].id),
            ],
        )
        await uow.commit()

    async with uow_factory() as uow:
        assert len(await uow.citations.for_messages(alice, [reply.id])) == 1
        assert len(await uow.citations.for_findings(alice, [finding.id])) == 1
        assert len(await uow.citations.for_dimensions(alice, [dimensions[0].id])) == 1
        assert await uow.citations.for_messages(bob, [reply.id]) == []
        assert await uow.citations.for_findings(bob, [finding.id]) == []
        assert await uow.citations.for_dimensions(bob, [dimensions[0].id]) == []
