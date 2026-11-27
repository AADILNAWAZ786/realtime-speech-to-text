from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from app.core.config import Settings
from app.db.session import check_database
from app.schemas.health import LivenessResponse, ReadinessResponse

router = APIRouter(prefix="/health", tags=["health"])


@router.get("", response_model=LivenessResponse, summary="Liveness probe")
async def liveness(request: Request) -> LivenessResponse:
    """Process is up. Does not touch the database (safe for frequent polling)."""
    settings: Settings = request.app.state.settings
    return LivenessResponse(version=settings.app_version, environment=settings.app_env.value)


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    responses={503: {"model": ReadinessResponse, "description": "A dependency is unavailable"}},
)
async def readiness(request: Request, response: Response) -> ReadinessResponse:
    """Ready to serve traffic: the database answers `SELECT 1`."""
    db_ok = await check_database(request.app.state.engine)
    if not db_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(
        status="ok" if db_ok else "unavailable",
        database="ok" if db_ok else "unavailable",
    )
