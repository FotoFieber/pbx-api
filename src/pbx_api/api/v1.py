# SPDX-License-Identifier: Apache-2.0
"""Version 1 of the business REST interface."""

from __future__ import annotations

import hmac
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..domain.models import (
    ActiveCall,
    Call,
    CallPage,
    CallSummary,
    Direction,
    Extension,
    Health,
    Line,
    Outcome,
    Problem,
    RingGroup,
)
from ..service import PbxService

_bearer = HTTPBearer(auto_error=False, description="Static bearer token configured on the server")

NOT_FOUND = {404: {"model": Problem, "description": "Not found"}}
UNAUTHORIZED = {401: {"model": Problem, "description": "Missing or invalid bearer token"}}
FORBIDDEN = {403: {"model": Problem, "description": "Client IP not in server.allowed_clients"}}


def get_service(request: Request) -> PbxService:
    return request.app.state.service


async def require_token(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    tokens: list[str] = request.app.state.tokens
    if not tokens:
        return  # authentication disabled (no tokens configured)
    supplied = creds.credentials if creds else ""
    if not any(hmac.compare_digest(supplied, t) for t in tokens):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid bearer token",
                            headers={"WWW-Authenticate": "Bearer"})


Service = Annotated[PbxService, Depends(get_service)]

public = APIRouter(prefix="/v1", responses=FORBIDDEN)
router = APIRouter(prefix="/v1", dependencies=[Depends(require_token)], responses={**UNAUTHORIZED, **FORBIDDEN})


@public.get("/health", response_model=Health, tags=["system"], summary="Service health")
async def health(svc: Service) -> Health:
    return await svc.health()


@router.get("/calls", response_model=CallPage, tags=["calls"], summary="Call history, newest first")
async def list_calls(
    svc: Service,
    from_: Annotated[datetime | None, Query(alias="from", description="Only calls starting at/after this time")] = None,
    to: Annotated[datetime | None, Query(description="Only calls starting before this time")] = None,
    before: Annotated[datetime | None, Query(description="Paging cursor: value of nextBefore")] = None,
    direction: Direction | None = None,
    outcome: Outcome | None = None,
    extension: Annotated[str | None, Query(description="Calls involving this extension")] = None,
    line: Annotated[str | None, Query(description="Calls on this line")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
) -> CallPage:
    return await svc.list_calls(
        start=from_, end=to, before=before, direction=direction, outcome=outcome,
        extension=extension, line=line, limit=limit,
    )


@router.get("/calls/summary", response_model=CallSummary, tags=["calls"], summary="Counts for a period")
async def calls_summary(
    svc: Service,
    from_: Annotated[datetime | None, Query(alias="from", description="Default: start of today")] = None,
    to: Annotated[datetime | None, Query(description="Default: end of today")] = None,
    group_by: Annotated[Literal["day", "line", "extension"] | None, Query()] = None,
) -> CallSummary:
    start, end = svc.today_range()
    return await svc.summary(from_ or start, to or end, group_by)


@router.get("/calls/active", response_model=list[ActiveCall], tags=["calls"], summary="Calls in progress")
async def active_calls(svc: Service) -> list[ActiveCall]:
    return svc.active_calls()


@router.get("/calls/{call_id}", response_model=Call, tags=["calls"], responses=NOT_FOUND, summary="One call")
async def get_call(svc: Service, call_id: str) -> Call:
    call = await svc.get_call(call_id)
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Call {call_id} not found")
    return call


@router.get("/extensions", response_model=list[Extension], tags=["extensions"], summary="All extensions")
async def list_extensions(svc: Service) -> list[Extension]:
    return svc.extensions()


@router.get("/extensions/{extension_id}", response_model=Extension, tags=["extensions"], responses=NOT_FOUND)
async def get_extension(svc: Service, extension_id: str) -> Extension:
    for ext in svc.extensions():
        if ext.id == extension_id:
            return ext
    raise HTTPException(status.HTTP_404_NOT_FOUND, f"Extension {extension_id} not found")


@router.get("/lines", response_model=list[Line], tags=["lines"], summary="External lines (trunks)")
async def list_lines(svc: Service) -> list[Line]:
    return svc.lines()


@router.get("/lines/{line_id}", response_model=Line, tags=["lines"], responses=NOT_FOUND)
async def get_line(svc: Service, line_id: str) -> Line:
    for line in svc.lines():
        if line.id == line_id:
            return line
    raise HTTPException(status.HTTP_404_NOT_FOUND, f"Line {line_id} not found")


@router.get("/ring-groups", response_model=list[RingGroup], tags=["lines"], summary="Ring groups")
async def list_ring_groups(svc: Service) -> list[RingGroup]:
    return svc.ring_groups()
