from __future__ import annotations

from fastapi import APIRouter, Request, status

from app.api.deps import AuthServiceDep, CurrentUser, LoginRateLimiterDep
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse
from app.schemas.common import ErrorResponse
from app.schemas.user import UserRead

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
    responses={409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
async def register(data: RegisterRequest, auth_service: AuthServiceDep) -> UserRead:
    user = await auth_service.register(data)
    return UserRead.model_validate(user)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Exchange email + password for an access token",
    responses={401: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
async def login(
    data: LoginRequest,
    request: Request,
    auth_service: AuthServiceDep,
    limiter: LoginRateLimiterDep,
) -> TokenResponse:
    # Behind a reverse proxy, run uvicorn with --proxy-headers so this is the real client IP.
    client_ip = request.client.host if request.client else "unknown"
    return await auth_service.login(
        data.email, data.password, limiter=limiter, client_key=f"{client_ip}|{data.email}"
    )


@router.get(
    "/me",
    response_model=UserRead,
    summary="The authenticated user",
    responses={401: {"model": ErrorResponse}},
)
async def me(current_user: CurrentUser) -> UserRead:
    return UserRead.model_validate(current_user)
