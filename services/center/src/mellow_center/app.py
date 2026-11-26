"""FastAPI transport. Production entry unlocks a vault before constructing the app."""

from typing import Literal
from uuid import uuid4
from datetime import datetime, timezone
from math import ceil
from urllib.parse import parse_qsl, urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException
from .domain import Problem
from .console import COOKIE, CSP, check_browser
from .features import VERSION


class Health(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["locked"] = "locked"
    protocol_version: Literal["0.2"] = "0.2"
    center_id: Literal["development-center"] = "development-center"
    center_epoch: Literal[1] = 1


def create_app(vault=None, lifespan=None) -> FastAPI:
    if vault is not None:
        from .space_migration import migrate_owner_spaces
        migrate_owner_spaces(vault)
        from .accounts import lifespan as accounts_lifespan
        lifespan = accounts_lifespan(vault, lifespan)
    app = FastAPI(
        title="Mellow personal center",
        version=VERSION,
        description="Preview API is separate from the unimplemented draft v1 protocol.",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def private_responses(request: Request, call_next):
        if request.url.path.startswith(('/api/', '/console/')):
            query = parse_qsl(request.scope.get('query_string', b'').decode('latin-1'), keep_blank_values=True)
            if any(key == 'space' and value in {'life', 'work'} for key, value in query):
                from .spaces import normalize_space
                request.scope['query_string'] = urlencode([(key, normalize_space(value) if key == 'space' else value)
                                                           for key, value in query]).encode()
        if request.url.path.startswith('/api/preview/') and COOKIE in request.cookies and not request.headers.get('authorization'):
            try:
                check_browser(request)
            except Problem as error:
                return JSONResponse({'code':error.code}, status_code=error.status)
            request.scope['headers'] = [*request.scope['headers'],
                (b'authorization', ('Bearer ' + request.cookies[COOKIE]).encode('latin-1'))]
        response = await call_next(request)
        if vault is not None and response.status_code < 400:
            cookie = request.cookies.get(COOKIE)
            auth = request.headers.get('authorization', '') or ('Bearer ' + cookie if cookie else '')
            if auth.startswith('Bearer ') and len(auth) <= 160:
                from starlette.concurrency import run_in_threadpool
                from .owner_access import renew_access
                try:
                    renewed = await run_in_threadpool(renew_access, vault, auth[7:])
                except Exception as error:
                    from .maintenance import log_event
                    log_event('ACCESS_RENEW_FAILED', 'access_renew', error)
                    renewed = None
                has_cookie = any(header.startswith(COOKIE + '=') for header in response.headers.getlist('set-cookie'))
                if renewed and cookie == auth[7:] and not has_cookie:
                    age = max(0, ceil((datetime.fromisoformat(renewed) - datetime.now(timezone.utc)).total_seconds()))
                    response.set_cookie(COOKIE, cookie, max_age=age,
                                        secure=True, httponly=True, samesite='strict', path='/')
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = CSP
        response.headers['X-Frame-Options'] = 'DENY'
        return response

    @app.get("/v1/health", response_model=Health, operation_id="health")
    async def health() -> Health:
        # Process responds, but no identity/vault/data has been initialized.
        return Health()

    @app.exception_handler(Problem)
    async def problem(_request: Request, exc: Problem) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"code": exc.code, "request_id": str(uuid4())})

    @app.exception_handler(TypeError)
    @app.exception_handler(KeyError)
    @app.exception_handler(ValueError)
    async def invalid_input(_request: Request, _exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=400, content={"code": "INVALID_ARGUMENT", "request_id": str(uuid4())})

    if vault is not None:
        from .api import router
        app.include_router(router(vault))
        from .console import router as console_router
        app.include_router(console_router(vault))
        from .downloads import router as downloads_router
        app.include_router(downloads_router(vault))
        from .admin import router as admin_router
        from .connector_gateway import router as connector_router
        app.include_router(admin_router(vault))
        app.include_router(connector_router(vault))
        from .accounts import routes as accounts_routes
        app.include_router(accounts_routes(vault))
        from .archive import router as archive_router
        from .legacy import router as legacy_router
        from .digital_self import router as digital_self_router
        app.include_router(archive_router(vault))
        app.include_router(legacy_router(vault))
        app.include_router(digital_self_router(vault))

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            headers=exc.headers,
            content={
                "request_id": str(uuid4()),
                "code": "NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR",
                "message": "Route unavailable in development bootstrap.",
                "retryable": False,
                "retry_after_seconds": None,
                "job_id": None,
            },
        )

    return app
