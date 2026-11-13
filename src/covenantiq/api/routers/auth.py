import hashlib
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status
from fastapi.security import OAuth2PasswordRequestForm

from covenantiq.api.deps import ContainerDep, CurrentPrincipal, rate_limit
from covenantiq.api.schemas.auth import LoginRequest, RegisterRequest, TokenOut, UserOut
from covenantiq.api.schemas.common import ErrorResponse
from covenantiq.services.auth_service import normalize_email

router = APIRouter(prefix="/auth", tags=["auth"])

ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
}


def _email_key(email: str) -> str:
    """Rate-limit identifier for an email: hashed, so addresses never appear in Redis keys."""
    return hashlib.sha256(normalize_email(email).encode()).hexdigest()[:32]


async def _login(request: Request, container: ContainerDep, email: str, password: str) -> TokenOut:
    client_ip = request.client.host if request.client else "unknown"
    await container.rate_limiter.hit("auth_login_ip", client_ip)
    await container.rate_limiter.hit("auth_login_email", _email_key(email))
    token = await container.auth_service.login(email=email, password=password)
    return TokenOut(access_token=token.access_token, expires_in=token.expires_in)


@router.post(
    "/register", response_model=UserOut, status_code=status.HTTP_201_CREATED, responses=ERRORS
)
async def register(payload: RegisterRequest, container: ContainerDep) -> UserOut:
    user = await container.auth_service.register(
        email=payload.email, password=payload.password, full_name=payload.full_name
    )
    return UserOut.model_validate(user)


@router.post("/login", response_model=TokenOut, responses=ERRORS)
async def login(payload: LoginRequest, request: Request, container: ContainerDep) -> TokenOut:
    return await _login(request, container, payload.email, payload.password)


@router.post("/token", response_model=TokenOut, responses=ERRORS, include_in_schema=True)
async def login_form(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    request: Request,
    container: ContainerDep,
) -> TokenOut:
    """The same login for OAuth2 clients, including the Authorize button in /docs (the email
    goes in the `username` field)."""
    return await _login(request, container, form.username, form.password)


@router.get(
    "/me",
    response_model=UserOut,
    responses=ERRORS,
    dependencies=[Depends(rate_limit("default"))],
)
async def me(principal: CurrentPrincipal, container: ContainerDep) -> UserOut:
    return UserOut.model_validate(await container.auth_service.get_me(principal))
