"""Link CRUD. Routes are thin adapters — validation and persistence live in
app/services/links.py."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, HttpUrl, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import client_ip, get_redis, get_session, get_settings_dep
from app.config import Settings
from app.core.rate_limit import RateLimiter, RateLimitUnavailable
from app.core.url_validation import URLValidationError
from app.models.link import Link
from app.services import links as links_service

router = APIRouter(prefix="/api/v1/links", tags=["links"])


def _reject_naive_datetime(value: datetime | None) -> datetime | None:
    # A naive value's UTC-ness would depend on the Postgres session's TimeZone GUC
    # rather than being explicit, per invariant 6.
    if value is not None and value.tzinfo is None:
        raise ValueError("expires_at must include a timezone offset")
    return value


def _unset_field(**kwargs: object) -> object:
    # `...` (Ellipsis), matching app.services.links.update_link's sentinel exactly —
    # they must be the *same* singleton, or "not provided" here silently becomes
    # "set the column to this object" there. default_factory, not a literal
    # default: a literal default must be JSON-serializable for the OpenAPI schema,
    # and Ellipsis is not.
    return Field(default_factory=lambda: ..., **kwargs)  # type: ignore[call-overload]


class LinkCreateRequest(BaseModel):
    target_url: HttpUrl
    custom_alias: str | None = Field(default=None, min_length=4, max_length=32)
    title: str | None = Field(default=None, max_length=255)
    description: str | None = None
    expires_at: datetime | None = None

    _validate_expires_at = field_validator("expires_at")(_reject_naive_datetime)


class LinkUpdateRequest(BaseModel):
    target_url: HttpUrl | None = None
    title: str | None = _unset_field(max_length=255)  # type: ignore[assignment]
    description: str | None = _unset_field()  # type: ignore[assignment]
    expires_at: datetime | None = _unset_field()  # type: ignore[assignment]
    is_active: bool | None = None

    _validate_expires_at = field_validator("expires_at")(_reject_naive_datetime)


class LinkResponse(BaseModel):
    id: uuid.UUID
    short_code: str
    short_url: str
    target_url: str
    title: str | None
    description: str | None
    is_active: bool
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @staticmethod
    def from_link(link: Link, *, public_base_url: str) -> LinkResponse:
        return LinkResponse(
            id=link.id,
            short_code=link.short_code,
            short_url=f"{public_base_url.rstrip('/')}/{link.short_code}",
            target_url=link.target_url,
            title=link.title,
            description=link.description,
            is_active=link.is_active,
            expires_at=link.expires_at,
            created_at=link.created_at,
            updated_at=link.updated_at,
        )


class LinkListResponse(BaseModel):
    items: list[LinkResponse]


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


@router.post("", response_model=LinkResponse, status_code=status.HTTP_201_CREATED)
async def create_link(
    request: Request,
    body: LinkCreateRequest,
    session: SessionDep,
    settings: SettingsDep,
    redis: Annotated[aioredis.Redis, Depends(get_redis)],
) -> LinkResponse:
    limiter = RateLimiter(
        redis=redis, key_prefix="rl:create", capacity=settings.rate_limit_create_per_minute
    )
    try:
        allowed = await limiter.allow(client_ip(request))
    except RateLimitUnavailable as exc:
        # Fails CLOSED: this is not the hot path, and an unthrottled create
        # endpoint with no auth yet is a public open-redirect factory.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="rate limiter unavailable",
        ) from exc
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="rate limit exceeded"
        )

    data = links_service.LinkCreate(
        target_url=str(body.target_url),
        custom_alias=body.custom_alias,
        title=body.title,
        description=body.description,
        expires_at=body.expires_at,
    )
    try:
        link = await links_service.create_link(session, data, public_host=settings.public_host)
    except URLValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except links_service.InvalidAliasError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except links_service.LinkAliasTakenError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="alias already in use"
        ) from exc
    except links_service.LinkCreationExhaustedError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="could not allocate a short code",
        ) from exc

    return LinkResponse.from_link(link, public_base_url=str(settings.public_base_url))


@router.get("", response_model=LinkListResponse)
async def list_links(
    session: SessionDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LinkListResponse:
    items = await links_service.list_links(session, limit=limit, offset=offset)
    return LinkListResponse(
        items=[
            LinkResponse.from_link(link, public_base_url=str(settings.public_base_url))
            for link in items
        ]
    )


@router.get("/{link_id}", response_model=LinkResponse)
async def get_link(
    link_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
) -> LinkResponse:
    try:
        link = await links_service.get_link(session, link_id)
    except links_service.LinkNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="link not found") from exc
    return LinkResponse.from_link(link, public_base_url=str(settings.public_base_url))


@router.patch("/{link_id}", response_model=LinkResponse)
async def update_link(
    link_id: uuid.UUID,
    body: LinkUpdateRequest,
    session: SessionDep,
    settings: SettingsDep,
) -> LinkResponse:
    try:
        link = await links_service.update_link(
            session,
            link_id,
            target_url=str(body.target_url) if body.target_url is not None else None,
            title=body.title,
            description=body.description,
            expires_at=body.expires_at,
            is_active=body.is_active,
            public_host=settings.public_host,
        )
    except links_service.LinkNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="link not found") from exc
    except URLValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    return LinkResponse.from_link(link, public_base_url=str(settings.public_base_url))


@router.delete("/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_link(
    link_id: uuid.UUID,
    session: SessionDep,
) -> None:
    try:
        await links_service.soft_delete(session, link_id)
    except links_service.LinkNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="link not found") from exc
