from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer

from covenantiq.api.container import Container
from covenantiq.core.errors import Unauthenticated
from covenantiq.core.logging import bind_context
from covenantiq.services.dto import Principal

# auto_error is off so a missing token goes through our error format (401 UNAUTHENTICATED).
# tokenUrl lets the Authorize button in /docs log in through the form endpoint.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


async def get_current_principal(
    container: ContainerDep, token: Annotated[str | None, Depends(oauth2_scheme)]
) -> Principal:
    if not token:
        raise Unauthenticated("Authentication required.")
    principal = await container.auth_service.authenticate(token)
    bind_context(user_id=str(principal.user_id))
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


def rate_limit(bucket: str) -> Callable[..., Awaitable[None]]:
    """A per-user rate limit dependency: `dependencies=[Depends(rate_limit("upload"))]`."""

    async def dependency(container: ContainerDep, principal: CurrentPrincipal) -> None:
        await container.rate_limiter.hit(bucket, str(principal.user_id))

    return dependency
