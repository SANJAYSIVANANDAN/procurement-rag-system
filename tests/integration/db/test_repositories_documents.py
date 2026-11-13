import asyncio
from collections.abc import Callable
from uuid import uuid4

import pytest

from covenantiq.core.enums import DocumentStatus, JobStatus
from covenantiq.db.scope import OwnershipScope
from covenantiq.db.uow import UnitOfWork
from tests.integration.factories import (
    make_document,
    make_processing_job,
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


async def test_add_get_and_list(uow_factory: UowFactory) -> None:
    alice, _ = await two_scopes(uow_factory)
    async with uow_factory() as uow:
        vendor = await uow.vendors.add(alice, make_vendor(alice.owner_id))
        first = await uow.documents.add(
            alice, make_document(alice.owner_id, title="Acme MSA", vendor_id=vendor.id)
        )
        second = await uow.documents.add(alice, make_document(alice.owner_id, title="Other NDA"))
        await uow.commit()

    async with uow_factory() as uow:
        assert (await uow.documents.get(alice, first.id)) is not None
        assert [d.id for d in await uow.documents.list(alice)] == [second.id, first.id]
        assert [d.id for d in await uow.documents.list(alice, q="acme")] == [first.id]
        assert [d.id for d in await uow.documents.list(alice, vendor_id=vendor.id)] == [first.id]
        assert await uow.documents.list(alice, status=DocumentStatus.READY) == []
        assert len(await uow.documents.list(alice, limit=1)) == 1


async def test_like_wildcards_in_search_are_literal(uow_factory: UowFactory) -> None:
    alice, _ = await two_scopes(uow_factory)
    async with uow_factory() as uow:
        await uow.documents.add(alice, make_document(alice.owner_id, title="100% uptime"))
        await uow.documents.add(alice, make_document(alice.owner_id, title="Plain"))
        await uow.commit()
        assert len(await uow.documents.list(alice, q="%")) == 1
        assert len(await uow.documents.list(alice, q="_")) == 0


async def test_other_users_cannot_see_or_touch_a_document(uow_factory: UowFactory) -> None:
    alice, bob = await two_scopes(uow_factory)
    async with uow_factory() as uow:
        document = await uow.documents.add(alice, make_document(alice.owner_id))
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.documents.get(bob, document.id) is None
        assert await uow.documents.list(bob) == []
        assert await uow.documents.find_active_by_sha(bob, document.sha256) is None
        assert not await uow.documents.soft_delete(bob, document.id)
        assert await uow.documents.get(alice, document.id) is not None


async def test_adding_with_the_wrong_scope_is_refused(uow_factory: UowFactory) -> None:
    alice, bob = await two_scopes(uow_factory)
    async with uow_factory() as uow:
        with pytest.raises(ValueError, match="ownership scope"):
            await uow.documents.add(bob, make_document(alice.owner_id))


async def test_soft_delete_hides_the_document(uow_factory: UowFactory) -> None:
    alice, _ = await two_scopes(uow_factory)
    async with uow_factory() as uow:
        document = await uow.documents.add(alice, make_document(alice.owner_id))
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.documents.soft_delete(alice, document.id)
        assert not await uow.documents.soft_delete(alice, document.id)  # already deleted
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.documents.get(alice, document.id) is None
        assert await uow.documents.list(alice) == []
        assert await uow.documents.find_active_by_sha(alice, document.sha256) is None
        deleted = await uow.documents.get(alice, document.id, include_deleted=True)
        assert deleted is not None
        assert deleted.status is DocumentStatus.DELETED
        assert deleted.deleted_at is not None


async def test_status_cas_follows_the_expected_state(uow_factory: UowFactory) -> None:
    alice, _ = await two_scopes(uow_factory)
    async with uow_factory() as uow:
        document = await uow.documents.add(
            alice, make_document(alice.owner_id, status=DocumentStatus.QUEUED)
        )
        await uow.commit()

    async with uow_factory() as uow:
        assert not await uow.documents.system_set_status(
            document.id, [DocumentStatus.READY], DocumentStatus.FAILED
        )
        assert await uow.documents.system_set_status(
            document.id, [DocumentStatus.QUEUED], DocumentStatus.EXTRACTING, page_count=12
        )
        await uow.commit()

    async with uow_factory() as uow:
        stored = await uow.documents.system_get(document.id)
        assert stored is not None
        assert (stored.status, stored.page_count) == (DocumentStatus.EXTRACTING, 12)


async def test_concurrent_cas_has_exactly_one_winner(uow_factory: UowFactory) -> None:
    alice, _ = await two_scopes(uow_factory)
    async with uow_factory() as uow:
        document = await uow.documents.add(
            alice, make_document(alice.owner_id, status=DocumentStatus.QUEUED)
        )
        job = await uow.processing_jobs.create(
            alice, make_processing_job(alice.owner_id, document.id)
        )
        await uow.commit()

    async def claim_document() -> bool:
        async with uow_factory() as uow:
            won = await uow.documents.system_set_status(
                document.id, [DocumentStatus.QUEUED], DocumentStatus.EXTRACTING
            )
            await uow.commit()
            return won

    async def claim_job() -> bool:
        async with uow_factory() as uow:
            won = await uow.processing_jobs.system_transition(
                job.id, [JobStatus.QUEUED], JobStatus.RUNNING, celery_task_id=str(uuid4())
            )
            await uow.commit()
            return won

    document_results = await asyncio.gather(*(claim_document() for _ in range(8)))
    job_results = await asyncio.gather(*(claim_job() for _ in range(8)))
    assert document_results.count(True) == 1
    assert job_results.count(True) == 1


async def test_uow_discards_uncommitted_work(uow_factory: UowFactory) -> None:
    alice, _ = await two_scopes(uow_factory)
    async with uow_factory() as uow:
        await uow.documents.add(alice, make_document(alice.owner_id))
        # no commit

    with pytest.raises(RuntimeError):
        async with uow_factory() as uow:
            await uow.documents.add(alice, make_document(alice.owner_id))
            await uow.commit()
            raise RuntimeError("boom after commit")

    async with uow_factory() as uow:
        assert len(await uow.documents.list(alice)) == 1


async def test_vendor_search_update_and_isolation(uow_factory: UowFactory) -> None:
    alice, bob = await two_scopes(uow_factory)
    async with uow_factory() as uow:
        acme = await uow.vendors.add(alice, make_vendor(alice.owner_id, "Acme Corp"))
        await uow.vendors.add(alice, make_vendor(alice.owner_id, "Globex"))
        await uow.commit()

    async with uow_factory() as uow:
        assert [v.name for v in await uow.vendors.list(alice, q="acm")] == ["Acme Corp"]
        assert await uow.vendors.list(bob) == []
        assert await uow.vendors.update(bob, acme.id, industry="Hacking") is None
        updated = await uow.vendors.update(alice, acme.id, industry="Software")
        assert updated is not None
        assert updated.industry == "Software"
        await uow.commit()
