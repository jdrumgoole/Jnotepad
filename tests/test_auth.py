from __future__ import annotations

import pytest

from tests.conftest import login, make_user


async def test_register_login_me(anon):
    username = await make_user(anon)
    token = await login(anon, username)
    r = await anon.get("/api/session/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["username"] == username


async def test_username_is_case_insensitive(anon):
    username = await make_user(anon)
    await login(anon, username.upper())


async def test_duplicate_username_rejected(anon):
    username = await make_user(anon)
    r = await anon.post("/api/session/register",
                        json={"username": username, "password": "another-pass"})
    assert r.status_code == 409
    assert "username" in r.json()["detail"]


@pytest.mark.parametrize("bad", ["ab", "-joe", "joe smith", "joe@example.com", "a" * 33, "jo..e"])
async def test_invalid_usernames(anon, bad):
    r = await anon.post("/api/session/register", json={"username": bad, "password": "longenough"})
    assert r.status_code == 422


async def test_wrong_password_message_mentions_username(anon):
    username = await make_user(anon)
    r = await anon.post("/api/session/login", json={"username": username, "password": "wrong-pass"})
    assert r.status_code == 401
    assert "username or password" in r.json()["detail"]
    assert "email" not in r.json()["detail"]


async def test_lockout_after_repeated_failures(anon):
    username = await make_user(anon)
    codes = []
    for _ in range(7):
        r = await anon.post("/api/session/login",
                            json={"username": username, "password": "wrong-pass"})
        codes.append(r.status_code)
    assert codes[-1] == 429
    assert "retry-after" not in {k.lower() for k in r.headers} or int(r.headers["retry-after"]) > 0


async def test_unauthenticated_requests_rejected(anon):
    assert (await anon.get("/api/items")).status_code == 401
    assert (await anon.get("/api/session/me")).status_code == 401


async def test_logout_revokes_token(anon):
    username = await make_user(anon)
    token = await login(anon, username)
    h = {"Authorization": f"Bearer {token}"}
    assert (await anon.post("/api/session/logout", headers=h)).status_code == 204
    assert (await anon.get("/api/session/me", headers=h)).status_code == 401


async def test_change_password(anon):
    username = await make_user(anon)
    token = await login(anon, username)
    h = {"Authorization": f"Bearer {token}"}
    r = await anon.post("/api/session/password", headers=h,
                        json={"current_password": "correct-horse", "new_password": "battery-staple"})
    assert r.status_code == 204, r.text
    assert (await anon.get("/api/session/me", headers=h)).status_code == 401  # old token revoked
    await login(anon, username, "battery-staple")


async def test_regstack_email_routes_not_public(anon):
    # regstack's own router is only mounted on the private sub-app.
    for path in ("/api/auth/register", "/api/auth/login", "/auth/login", "/auth/change-email"):
        assert (await anon.post(path, json={})).status_code in (404, 405)
