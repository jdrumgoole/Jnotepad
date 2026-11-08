"""Username/password auth on top of regstack.

regstack only knows email addresses. Rather than re-implement its password
hashing, lockout and JWT revocation, these endpoints translate a username into
a synthetic email and forward the request to regstack's own JSON router, which
is mounted on a private ASGI sub-app that is *not* reachable from outside. That
keeps regstack's email-centric routes (change-email, forgot-password, ...) off
the public surface, where they would make no sense for username accounts.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from pydantic import AfterValidator, BaseModel, Field
from regstack import RegStack

from jnotepad.config import USERNAME_EMAIL_DOMAIN

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,31}$")


def _normalise_username(value: str) -> str:
    value = value.strip().lower()
    if not USERNAME_RE.fullmatch(value) or ".." in value or value.endswith("."):
        raise ValueError(
            "Username must be 3-32 characters: letters, digits, '.', '_' or '-', "
            "starting with a letter or digit."
        )
    return value


Username = Annotated[str, AfterValidator(_normalise_username)]
Password = Annotated[str, Field(min_length=8, max_length=128)]


def username_to_email(username: str) -> str:
    return f"{username}@{USERNAME_EMAIL_DOMAIN}"


def email_to_username(email: str) -> str:
    local, _, domain = email.rpartition("@")
    return local if domain == USERNAME_EMAIL_DOMAIN else email


class Credentials(BaseModel):
    username: Username
    password: Password


class ChangePassword(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: Password


class UserOut(BaseModel):
    id: str
    username: str
    created_at: Any = None
    last_login: Any = None


def build_internal_auth_app(rs: RegStack) -> FastAPI:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.include_router(rs.router, prefix="/auth")
    return app


def _rewrite_detail(detail: Any) -> Any:
    if isinstance(detail, str):
        return detail.replace("email or password", "username or password").replace(
            "with that email", "with that username"
        )
    return detail


def build_session_router(rs: RegStack) -> APIRouter:
    router = APIRouter(prefix="/api/session", tags=["session"])
    current_user = rs.deps.current_user()

    def client(request: Request) -> httpx.AsyncClient:
        return request.app.state.auth_client

    async def forward(
        c: httpx.AsyncClient, method: str, path: str, *, json: Any = None,
        auth: str | None = None,
    ) -> httpx.Response:
        headers = {"Authorization": auth} if auth else {}
        resp = await c.request(method, f"/auth{path}", json=json, headers=headers)
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            extra = {"Retry-After": resp.headers["retry-after"]} if (
                "retry-after" in resp.headers) else None
            raise HTTPException(resp.status_code, _rewrite_detail(detail), headers=extra)
        return resp

    @router.post("/register", status_code=201, response_model=UserOut)
    async def register(creds: Credentials, c: httpx.AsyncClient = Depends(client)):
        if not rs.config.allow_registration:
            raise HTTPException(403, "Registration is disabled.")
        resp = await forward(c, "POST", "/register", json={
            "email": username_to_email(creds.username), "password": creds.password,
            "full_name": creds.username,
        })
        body = resp.json()
        return UserOut(id=body["id"], username=creds.username, created_at=body.get("created_at"))

    @router.post("/login")
    async def login(creds: Credentials, c: httpx.AsyncClient = Depends(client)):
        resp = await forward(c, "POST", "/login", json={
            "email": username_to_email(creds.username), "password": creds.password,
        })
        body = resp.json()
        if "access_token" not in body:  # e.g. MFA — never enabled in this app
            raise HTTPException(501, "Unsupported login flow.")
        return {**body, "username": creds.username}

    @router.post("/logout", status_code=204)
    async def logout(request: Request, c: httpx.AsyncClient = Depends(client),
                     _user=Depends(current_user)):
        await forward(c, "POST", "/logout", auth=request.headers.get("authorization"))
        return Response(status_code=204)

    @router.get("/me", response_model=UserOut)
    async def me(user=Depends(current_user)):
        return UserOut(id=str(user.id), username=email_to_username(user.email),
                       created_at=user.created_at, last_login=user.last_login)

    @router.post("/password", status_code=204)
    async def change_password(body: ChangePassword, request: Request,
                              c: httpx.AsyncClient = Depends(client),
                              _user=Depends(current_user)):
        # regstack revokes every existing token for the user on success.
        await forward(c, "POST", "/change-password", json=body.model_dump(),
                      auth=request.headers.get("authorization"))
        return Response(status_code=204)

    return router
