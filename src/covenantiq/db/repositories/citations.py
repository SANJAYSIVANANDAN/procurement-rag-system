from collections.abc import Collection, Sequence
from uuid import UUID

from sqlalchemy import select

from covenantiq.db.models import (
    Citation,
    ContractFinding,
    Conversation,
    Message,
    VendorRiskAssessment,
    VendorRiskDimension,
)
from covenantiq.db.repositories.base import Repository
from covenantiq.db.scope import OwnershipScope


class CitationRepository(Repository):
    async def bulk_add(self, scope: OwnershipScope, citations: Sequence[Citation]) -> None:
        """Parent ownership is verified by the calling service, which just loaded the parent
        through a scoped repository; the database enforces exactly one parent per citation."""
        self.session.add_all(citations)
        await self.session.flush()

    async def for_messages(
        self, scope: OwnershipScope, message_ids: Collection[UUID]
    ) -> Sequence[Citation]:
        statement = (
            select(Citation)
            .join(Message, Message.id == Citation.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Citation.message_id.in_(list(message_ids)))
            .order_by(Citation.marker, Citation.id)
        )
        statement = scope.apply(statement, Conversation)
        return (await self.session.execute(statement)).scalars().all()

    async def for_findings(
        self, scope: OwnershipScope, finding_ids: Collection[UUID]
    ) -> Sequence[Citation]:
        statement = (
            select(Citation)
            .join(ContractFinding, ContractFinding.id == Citation.finding_id)
            .where(Citation.finding_id.in_(list(finding_ids)))
            .order_by(Citation.id)
        )
        statement = scope.apply(statement, ContractFinding)
        return (await self.session.execute(statement)).scalars().all()

    async def for_dimensions(
        self, scope: OwnershipScope, dimension_ids: Collection[UUID]
    ) -> Sequence[Citation]:
        statement = (
            select(Citation)
            .join(VendorRiskDimension, VendorRiskDimension.id == Citation.risk_dimension_id)
            .join(
                VendorRiskAssessment,
                VendorRiskAssessment.id == VendorRiskDimension.assessment_id,
            )
            .where(Citation.risk_dimension_id.in_(list(dimension_ids)))
            .order_by(Citation.id)
        )
        statement = scope.apply(statement, VendorRiskAssessment)
        return (await self.session.execute(statement)).scalars().all()
