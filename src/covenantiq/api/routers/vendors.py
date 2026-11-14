from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from covenantiq.api.deps import ContainerDep, CurrentPrincipal, rate_limit
from covenantiq.api.schemas.common import ErrorResponse
from covenantiq.api.schemas.documents import DocumentOut
from covenantiq.api.schemas.vendors import (
    VendorCreate,
    VendorDetail,
    VendorOut,
    VendorPage,
    VendorUpdate,
)

router = APIRouter(
    prefix="/vendors",
    tags=["vendors"],
    dependencies=[Depends(rate_limit("default"))],
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)


@router.post(
    "",
    response_model=VendorOut,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ErrorResponse}},
)
async def create_vendor(
    payload: VendorCreate, principal: CurrentPrincipal, container: ContainerDep
) -> VendorOut:
    vendor = await container.vendor_service.create(
        principal,
        name=payload.name,
        industry=payload.industry,
        website=payload.website,
        metadata=payload.metadata,
    )
    return VendorOut.model_validate(vendor)


@router.get("", response_model=VendorPage)
async def list_vendors(
    principal: CurrentPrincipal,
    container: ContainerDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=500)] = None,
) -> VendorPage:
    page = await container.vendor_service.list(principal, q=q, limit=limit, cursor=cursor)
    return VendorPage(
        items=[VendorOut.model_validate(item) for item in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/{vendor_id}", response_model=VendorDetail)
async def get_vendor(
    vendor_id: UUID, principal: CurrentPrincipal, container: ContainerDep
) -> VendorDetail:
    detail = await container.vendor_service.get(principal, vendor_id)
    return VendorDetail(
        **VendorOut.model_validate(detail.vendor).model_dump(),
        documents=[DocumentOut.model_validate(d) for d in detail.documents],
    )


@router.patch("/{vendor_id}", response_model=VendorOut, responses={409: {"model": ErrorResponse}})
async def update_vendor(
    vendor_id: UUID,
    payload: VendorUpdate,
    principal: CurrentPrincipal,
    container: ContainerDep,
) -> VendorOut:
    changes: dict[str, Any] = payload.model_dump(exclude_unset=True)
    vendor = await container.vendor_service.update(principal, vendor_id, changes)
    return VendorOut.model_validate(vendor)
