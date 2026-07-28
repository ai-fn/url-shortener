"""Liveness and readiness.

`/healthz` must never touch a dependency, or a Redis blip restarts every pod instead of
just removing them from the load balancer. Both handlers are adapters; the readiness
decision lives in app/services/health.py.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

from app.services.health import run_readiness_checks

router = APIRouter()


class HealthResponse(BaseModel):
    status: str


class ReadyResponse(BaseModel):
    status: str
    checks: dict[str, str]


# A cached 200 pins "ready" for a pod whose Postgres has since died.
_NO_STORE = "no-store"


@router.get("/healthz", response_model=HealthResponse)
async def healthz(response: Response) -> HealthResponse:
    """Liveness. Process is up. Deliberately checks nothing else."""
    response.headers["Cache-Control"] = _NO_STORE
    return HealthResponse(status="ok")


@router.get(
    "/readyz",
    response_model=ReadyResponse,
    # Declared so the one status code this endpoint exists to emit is in the schema.
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ReadyResponse,
            "description": "At least one required dependency is unavailable.",
        }
    },
)
async def readyz(request: Request, response: Response) -> ReadyResponse:
    """Readiness. Can this instance serve traffic?"""
    response.headers["Cache-Control"] = _NO_STORE
    report = await run_readiness_checks(request.app)

    if not report.ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadyResponse(
        status="ready" if report.ok else "not_ready",
        checks=report.checks,
    )
