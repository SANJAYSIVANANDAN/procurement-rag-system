"""Database-level rules (spec 15.3). These hold regardless of which code writes the rows."""

from collections.abc import Callable
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

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
    make_processing_job,
    make_user,
    make_vendor,
)

UowFactory = Callable[[], UnitOfWork]


async def seed_user(uow_factory: UowFactory) -> OwnershipScope:
    async with uow_factory() as uow:
        user = await uow.users.add(make_user())
        await uow.commit()
    return OwnershipScope(user.id)


async def test_email_is_unique_case_insensitively(uow_factory: UowFactory) -> None:
    async with uow_factory() as uow:
        await uow.users.add(make_user("Ada@Example.com"))
        await uow.commit()
    async with uow_factory() as uow:
        with pytest.raises(IntegrityError):
            await uow.users.add(make_user("ada@example.COM"))
    async with uow_factory() as uow:
        found = await uow.users.get_by_email("ADA@example.com")
        assert found is not None


async def test_duplicate_sha_is_rejected_only_among_active_documents(
    uow_factory: UowFactory,
) -> None:
    scope = await seed_user(uow_factory)
    sha = "a" * 64

    async with uow_factory() as uow:
        first = await uow.documents.add(scope, make_document(scope.owner_id, sha256=sha))
        await uow.commit()

    async with uow_factory() as uow:
        with pytest.raises(IntegrityError):
            await uow.documents.add(scope, make_document(scope.owner_id, sha256=sha))

    async with uow_factory() as uow:
        assert await uow.documents.soft_delete(scope, first.id)
        await uow.commit()

    async with uow_factory() as uow:
        again = await uow.documents.add(scope, make_document(scope.owner_id, sha256=sha))
        await uow.commit()
    assert again.id != first.id


async def test_same_bytes_may_be_uploaded_by_different_owners(uow_factory: UowFactory) -> None:
    scope_a = await seed_user(uow_factory)
    scope_b = await seed_user(uow_factory)
    async with uow_factory() as uow:
        await uow.documents.add(scope_a, make_document(scope_a.owner_id, sha256="b" * 64))
        await uow.documents.add(scope_b, make_document(scope_b.owner_id, sha256="b" * 64))
        await uow.commit()


async def test_document_size_must_be_positive(uow_factory: UowFactory) -> None:
    scope = await seed_user(uow_factory)
    async with uow_factory() as uow:
        with pytest.raises(IntegrityError):
            await uow.documents.add(scope, make_document(scope.owner_id, size_bytes=0))


async def test_only_one_active_processing_job_per_document(uow_factory: UowFactory) -> None:
    scope = await seed_user(uow_factory)
    async with uow_factory() as uow:
        document = await uow.documents.add(scope, make_document(scope.owner_id))
        job = await uow.processing_jobs.create(
            scope, make_processing_job(scope.owner_id, document.id)
        )
        await uow.commit()

    async with uow_factory() as uow:
        with pytest.raises(IntegrityError):
            await uow.processing_jobs.create(
                scope, make_processing_job(scope.owner_id, document.id)
            )

    async with uow_factory() as uow:
        assert await uow.processing_jobs.system_transition(
            job.id, [JobStatus.QUEUED], JobStatus.SUCCEEDED
        )
        await uow.commit()

    async with uow_factory() as uow:
        await uow.processing_jobs.create(scope, make_processing_job(scope.owner_id, document.id))
        await uow.commit()


async def test_message_sequence_and_client_id_are_unique(uow_factory: UowFactory) -> None:
    scope = await seed_user(uow_factory)
    client_id = uuid4()
    async with uow_factory() as uow:
        conversation = await uow.conversations.create(scope, make_conversation(scope.owner_id))
        locked = await uow.conversations.get_for_update(scope, conversation.id)
        assert locked is not None
        user_message, assistant_message = make_message_pair(client_message_id=client_id)
        await uow.messages.add_pair(scope, locked, user_message, assistant_message)
        await uow.commit()

    async with uow_factory() as uow:
        locked = await uow.conversations.get_for_update(scope, conversation.id)
        assert locked is not None
        duplicate, reply = make_message_pair(client_message_id=client_id)
        with pytest.raises(IntegrityError):
            await uow.messages.add_pair(scope, locked, duplicate, reply)

    async with uow_factory() as uow:
        locked = await uow.conversations.get_for_update(scope, conversation.id)
        assert locked is not None
        for _ in range(2):  # messages without a client id never collide on it
            first, second = make_message_pair()
            await uow.messages.add_pair(scope, locked, first, second)
        await uow.commit()
        messages = await uow.messages.list(scope, conversation.id)
    assert [m.seq for m in messages] == [1, 2, 3, 4, 5, 6]


async def test_analysis_job_target_must_match_its_type(uow_factory: UowFactory) -> None:
    scope = await seed_user(uow_factory)
    async with uow_factory() as uow:
        document = await uow.documents.add(scope, make_document(scope.owner_id))
        vendor = await uow.vendors.add(scope, make_vendor(scope.owner_id))
        await uow.commit()

    bad_jobs = [
        make_analysis_job(scope.owner_id),  # contract analysis without a document
        make_analysis_job(
            scope.owner_id, document_id=document.id, job_type=AnalysisJobType.VENDOR_ASSESSMENT
        ),  # vendor assessment without a vendor
    ]
    for bad in bad_jobs:
        async with uow_factory() as uow:
            with pytest.raises(IntegrityError):
                await uow.analysis_jobs.create_or_get_active(scope, bad)

    async with uow_factory() as uow:
        for job in (
            make_analysis_job(scope.owner_id, document_id=document.id),
            make_analysis_job(
                scope.owner_id, vendor_id=vendor.id, job_type=AnalysisJobType.VENDOR_ASSESSMENT
            ),
            make_analysis_job(scope.owner_id, job_type=AnalysisJobType.VENDOR_COMPARISON),
        ):
            _, created = await uow.analysis_jobs.create_or_get_active(scope, job)
            assert created
        await uow.commit()


async def test_citation_needs_exactly_one_parent(uow_factory: UowFactory) -> None:
    scope = await seed_user(uow_factory)
    async with uow_factory() as uow:
        document = await uow.documents.add(scope, make_document(scope.owner_id))
        conversation = await uow.conversations.create(scope, make_conversation(scope.owner_id))
        locked = await uow.conversations.get_for_update(scope, conversation.id)
        assert locked is not None
        _, reply = await uow.messages.add_pair(scope, locked, *make_message_pair())
        job, _ = await uow.analysis_jobs.create_or_get_active(
            scope, make_analysis_job(scope.owner_id, document_id=document.id)
        )
        finding = make_finding(scope.owner_id, job.id, document.id)
        await uow.findings.bulk_add(scope, [finding])
        await uow.commit()

    async with uow_factory() as uow:
        with pytest.raises(IntegrityError):
            await uow.citations.bulk_add(scope, [make_citation(document.id)])  # no parent

    async with uow_factory() as uow:
        with pytest.raises(IntegrityError):
            await uow.citations.bulk_add(
                scope,
                [make_citation(document.id, message_id=reply.id, finding_id=finding.id)],  # two
            )

    async with uow_factory() as uow:
        await uow.citations.bulk_add(scope, [make_citation(document.id, message_id=reply.id)])
        await uow.citations.bulk_add(scope, [make_citation(document.id, finding_id=finding.id)])
        await uow.commit()


async def test_vendor_has_at_most_one_latest_assessment(uow_factory: UowFactory) -> None:
    scope = await seed_user(uow_factory)
    async with uow_factory() as uow:
        vendor = await uow.vendors.add(scope, make_vendor(scope.owner_id))
        job, _ = await uow.analysis_jobs.create_or_get_active(
            scope,
            make_analysis_job(
                scope.owner_id, vendor_id=vendor.id, job_type=AnalysisJobType.VENDOR_ASSESSMENT
            ),
        )
        first, _ = make_assessment(scope.owner_id, vendor.id, job.id)
        second, _ = make_assessment(scope.owner_id, vendor.id, job.id)
        uow.session.add(first)
        await uow.session.flush()
        uow.session.add(second)  # bypasses the repository, which would demote `first`
        with pytest.raises(IntegrityError):
            await uow.session.flush()


async def test_vendor_names_are_unique_per_owner(uow_factory: UowFactory) -> None:
    scope_a = await seed_user(uow_factory)
    scope_b = await seed_user(uow_factory)
    async with uow_factory() as uow:
        await uow.vendors.add(scope_a, make_vendor(scope_a.owner_id, "Acme"))
        await uow.vendors.add(scope_b, make_vendor(scope_b.owner_id, "Acme"))
        await uow.commit()
    async with uow_factory() as uow:
        with pytest.raises(IntegrityError):
            await uow.vendors.add(scope_a, make_vendor(scope_a.owner_id, "Acme"))
