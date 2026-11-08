"""FastAPI application factory."""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from importlib.resources import files

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pymongo import AsyncMongoClient
from regstack import RegStack, RegStackConfig

from jnotepad.auth import build_internal_auth_app, build_session_router
from jnotepad.config import Settings
from jnotepad.db import database_server, install_indexes
from jnotepad.items import build_items_router

STATIC_DIR = files("jnotepad") / "static"

# All user content is rendered with textContent in the UI; this CSP is the
# backstop. Images are loaded via fetch() + blob: URLs because the API uses
# bearer tokens, not cookies.
CSP = (
    "default-src 'self'; img-src 'self' blob: data:; script-src 'self'; "
    "style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'"
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with AsyncExitStack() as stack:
            base_uri = stack.enter_context(database_server(settings))
            rs = RegStack(config=RegStackConfig(
                app_name="Jnotepad",
                base_url=settings.base_url,
                database_url=f"{base_uri}/{settings.database}",
                jwt_secret=settings.resolved_jwt_secret,
                jwt_ttl_seconds=settings.jwt_ttl_seconds,
                # Username accounts have no real mailbox, so every email-driven
                # flow is off. Login lockout stays on (per username).
                require_verification=False,
                enable_password_reset=False,
                enable_account_deletion=False,
                allow_registration=settings.allow_registration,
            ))
            await rs.install_schema()
            stack.push_async_callback(rs.aclose)

            mongo = AsyncMongoClient(f"{base_uri}/", tz_aware=True)
            stack.push_async_callback(mongo.close)
            db = mongo[settings.database]
            await install_indexes(db)

            auth_client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=build_internal_auth_app(rs)),
                base_url="http://regstack.internal",
            )
            stack.push_async_callback(auth_client.aclose)

            app.state.settings = settings
            app.state.db = db
            app.state.auth_client = auth_client
            app.include_router(build_session_router(rs))
            app.include_router(build_items_router(rs))
            yield

    app = FastAPI(title="Jnotepad", lifespan=lifespan)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(str(STATIC_DIR / "index.html"),
                            headers={"Cache-Control": "no-cache"})

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True}

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app
