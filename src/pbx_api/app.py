# SPDX-License-Identifier: Apache-2.0
"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__
from .api import v1
from .service import PbxService

DESCRIPTION = """
Business-level access to a FreePBX/Asterisk phone system: calls as people
experience them (not CDR legs), extensions, external lines and ring groups.

All timestamps are RFC 3339 with offset. Authentication: static bearer token.
"""


def _problem(status: int, title: str, detail: str | None = None) -> JSONResponse:
    body = {"type": "about:blank", "title": title, "status": status}
    if detail:
        body["detail"] = detail
    return JSONResponse(body, status_code=status, media_type="application/problem+json")


def create_app(
    service: PbxService,
    tokens: list[str] | None = None,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def _noop(_: FastAPI) -> AsyncIterator[None]:
        yield

    app = FastAPI(
        title="pbx-api",
        version=__version__,
        description=DESCRIPTION,
        license_info={"name": "Apache-2.0", "identifier": "Apache-2.0"},
        lifespan=lifespan or _noop,
    )
    app.state.service = service
    app.state.tokens = tokens or []
    app.include_router(v1.public)
    app.include_router(v1.router)

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        resp = _problem(exc.status_code, str(exc.detail))
        for k, v in (exc.headers or {}).items():
            resp.headers[k] = v
        return resp

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        detail = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        return _problem(422, "Invalid request", detail)

    return app
