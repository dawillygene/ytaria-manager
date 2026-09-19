from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .api import auth, files, jobs
from .config import get_settings
from .db import get_engine
from .logging_utils import configure_logging
from .services.errors import ServiceError

log = logging.getLogger("ytaria.api")


class BodyLimitMiddleware:
    """Reject request bodies larger than the configured limit, including chunked uploads."""

    def __init__(self, app, max_bytes: int) -> None:
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        declared = headers.get(b"content-length")
        too_large = JSONResponse({"error": {"code": "body_too_large", "message": "Request body is too large."}}, status_code=413)
        if declared is not None:
            try:
                if int(declared) > self.max_bytes:
                    return await too_large(scope, receive, send)
            except ValueError:
                return await JSONResponse({"error": {"code": "bad_request", "message": "Bad Content-Length."}}, status_code=400)(scope, receive, send)
        received = 0

        async def limited():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise HTTPException(status_code=413, detail={"code": "body_too_large", "message": "Request body is too large."})
            return message

        await self.app(scope, limited, send)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        log.info("api starting env=%s", settings.env)
        yield
        log.info("api stopping")

    app = FastAPI(
        title="ytaria-manager API", version="1.0.0", lifespan=lifespan,
        docs_url="/api/docs" if settings.docs_enabled else None,
        redoc_url=None, openapi_url="/api/openapi.json" if settings.docs_enabled else None,
    )

    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes)
    app.add_middleware(
        CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
        allow_methods=["GET", "HEAD", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-CSRF-Token", "X-Client", "X-Requested-With", "Range", "If-Range"],
        expose_headers=["Content-Range", "Accept-Ranges", "Content-Disposition", "ETag", "Retry-After"], max_age=600,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = uuid.uuid4().hex[:12]
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("unhandled error", extra={"request_id": rid})
            response = JSONResponse({"error": {"code": "internal_error", "message": "Something went wrong."}}, status_code=500)
        response.headers["X-Request-ID"] = rid
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-site")
        if request.url.path.startswith("/api") and "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)  # route template: never log ticket values
        log.info("%s %s -> %s %dms", request.method, path, response.status_code, (time.monotonic() - started) * 1000,
                 extra={"request_id": rid})
        return response

    @app.exception_handler(ServiceError)
    async def service_error(_: Request, exc: ServiceError):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status_code)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": f"http_{exc.status_code}", "message": str(exc.detail)}
        return JSONResponse({"error": detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        fields = [{"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()]
        return JSONResponse({"error": {"code": "validation_error", "message": "Some fields are invalid.", "fields": fields}}, status_code=422)

    for router in (auth.router, jobs.router, files.router):
        app.include_router(router, prefix="/api")

    @app.get("/api/config", tags=["ops"])
    def public_config():
        """Non-sensitive settings the sign-in screen needs before authentication."""
        return {"app": "ytaria-manager", "registration": settings.registration_mode, "password_min_length": settings.password_min_length}

    @app.get("/api/health", tags=["ops"])
    def health():
        return {"status": "ok"}

    @app.get("/api/ready", tags=["ops"])
    def ready():
        checks = {}
        try:
            with get_engine().connect() as c:
                c.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception:
            checks["database"] = "error"
        try:
            import redis

            redis.Redis.from_url(settings.redis_url, socket_timeout=2, socket_connect_timeout=2).ping()
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "error"
        ok = all(v == "ok" for v in checks.values())
        return JSONResponse({"status": "ok" if ok else "degraded", **checks}, status_code=200 if ok else 503)

    return app


app = create_app()
