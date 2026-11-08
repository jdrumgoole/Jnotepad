from __future__ import annotations

import itertools

import httpx
import pytest

from jnotepad.config import Settings
from jnotepad.main import create_app

_counter = itertools.count()


@pytest.fixture(scope="session")
async def app(tmp_path_factory):
    """One embedded SecantusDB server for the whole run (startup is the slow part)."""
    settings = Settings(data_dir=tmp_path_factory.mktemp("data"), max_image_bytes=64 * 1024)
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def anon(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        yield c


async def make_user(client: httpx.AsyncClient, password: str = "correct-horse") -> str:
    username = f"user{next(_counter)}"
    r = await client.post("/api/session/register", json={"username": username,
                                                           "password": password})
    assert r.status_code == 201, r.text
    return username


async def login(client: httpx.AsyncClient, username: str, password: str = "correct-horse"):
    r = await client.post("/api/session/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture
async def client(app):
    """A client already logged in as a fresh user."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        username = await make_user(c)
        c.headers["Authorization"] = f"Bearer {await login(c, username)}"
        c.username = username  # type: ignore[attr-defined]
        yield c
