from __future__ import annotations

import base64

import httpx

from tests.conftest import login, make_user

# 1x1 transparent PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


async def test_note_link_image_lifecycle(client):
    r = await client.post("/api/items", json={"title": "Groceries", "text": "eggs\nmilk",
                                              "tags": ["#Home", "home", "todo"]})
    assert r.status_code == 201, r.text
    note = r.json()
    assert note["kind"] == "note"
    assert note["tags"] == ["home", "todo"]

    r = await client.post("/api/items", json={"url": "https://example.com/a?b=1"})
    link = r.json()
    assert link["kind"] == "link"

    r = await client.post("/api/items", json={"title": "Screenshot"})
    shot = r.json()
    r = await client.post(f"/api/items/{shot['id']}/images",
                          files={"file": ("s.png", PNG, "application/octet-stream")})
    assert r.status_code == 201, r.text
    shot = r.json()
    assert shot["kind"] == "image"
    img = shot["images"][0]
    assert img["content_type"] == "image/png"  # sniffed, not trusted from the client

    r = await client.get(f"/api/images/{img['id']}")
    assert r.status_code == 200
    assert r.content == PNG
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"

    page = (await client.get("/api/items")).json()
    assert page["total"] == 3
    assert [i["id"] for i in page["items"]][0] == shot["id"]  # most recently updated first

    for kind, expected in (("note", note["id"]), ("link", link["id"]), ("image", shot["id"])):
        got = (await client.get("/api/items", params={"kind": kind})).json()["items"]
        assert [i["id"] for i in got] == [expected], kind

    assert (await client.get("/api/tags")).json() == ["home", "todo"]

    r = await client.delete(f"/api/items/{shot['id']}")
    assert r.status_code == 204
    assert (await client.get(f"/api/images/{img['id']}")).status_code == 404


async def test_search_is_literal_and_case_insensitive(client):
    await client.post("/api/items", json={"title": "Python (3.13) notes", "text": "asyncio"})
    await client.post("/api/items", json={"title": "Other", "tags": ["rust"]})
    hits = (await client.get("/api/items", params={"q": "(3.13)"})).json()["items"]
    assert [h["title"] for h in hits] == ["Python (3.13) notes"]
    assert len((await client.get("/api/items", params={"q": "ASYNCIO"})).json()["items"]) == 1
    assert len((await client.get("/api/items", params={"q": "rus"})).json()["items"]) == 1
    # A regex metacharacter pattern must not blow up or match everything.
    assert (await client.get("/api/items", params={"q": ".*"})).json()["items"] == []
    assert len((await client.get("/api/items", params={"tag": "rust"})).json()["items"]) == 1


async def test_update_and_clear_url(client):
    item = (await client.post("/api/items", json={"title": "x", "url": "http://a.example"})).json()
    r = await client.patch(f"/api/items/{item['id']}", json={"text": "more"})
    assert r.json()["url"] == "http://a.example"
    assert r.json()["text"] == "more"
    r = await client.patch(f"/api/items/{item['id']}", json={"url": None})
    assert r.json()["url"] is None
    assert r.json()["kind"] == "note"


async def test_rejects_dangerous_urls(client):
    for url in ("javascript:alert(1)", "data:text/html,hi", "ftp://x", "not a url"):
        r = await client.post("/api/items", json={"url": url})
        assert r.status_code == 422, url


async def test_rejects_non_images_and_oversize(client):
    item = (await client.post("/api/items", json={"title": "x"})).json()
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    r = await client.post(f"/api/items/{item['id']}/images",
                          files={"file": ("x.svg", svg, "image/svg+xml")})
    assert r.status_code == 415
    big = PNG + b"\0" * (64 * 1024)
    r = await client.post(f"/api/items/{item['id']}/images",
                          files={"file": ("big.png", big, "image/png")})
    assert r.status_code == 413


async def test_remove_single_image(client):
    item = (await client.post("/api/items", json={"title": "x"})).json()
    for _ in range(2):
        item = (await client.post(f"/api/items/{item['id']}/images",
                                  files={"file": ("a.png", PNG, "image/png")})).json()
    first = item["images"][0]["id"]
    r = await client.delete(f"/api/items/{item['id']}/images/{first}")
    assert r.status_code == 200
    assert [i["id"] for i in r.json()["images"]] == [item["images"][1]["id"]]
    assert (await client.get(f"/api/images/{first}")).status_code == 404


async def test_users_cannot_see_each_others_data(client, app):
    item = (await client.post("/api/items", json={"title": "secret", "tags": ["private"]})).json()
    item = (await client.post(f"/api/items/{item['id']}/images",
                              files={"file": ("a.png", PNG, "image/png")})).json()
    image_id = item["images"][0]["id"]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as other:
        name = await make_user(other)
        other.headers["Authorization"] = f"Bearer {await login(other, name)}"
        assert (await other.get("/api/items")).json()["total"] == 0
        assert (await other.get("/api/tags")).json() == []
        assert (await other.get(f"/api/items/{item['id']}")).status_code == 404
        assert (await other.patch(f"/api/items/{item['id']}", json={"title": "pwn"})
                ).status_code == 404
        assert (await other.delete(f"/api/items/{item['id']}")).status_code == 404
        assert (await other.get(f"/api/images/{image_id}")).status_code == 404
        r = await other.post(f"/api/items/{item['id']}/images",
                             files={"file": ("a.png", PNG, "image/png")})
        assert r.status_code == 404

    assert (await client.get(f"/api/items/{item['id']}")).json()["title"] == "secret"


async def test_bad_ids_are_404(client):
    assert (await client.get("/api/items/not-an-id")).status_code == 404
    assert (await client.get("/api/images/zzz")).status_code == 404


async def test_index_served_with_csp(anon):
    r = await anon.get("/")
    assert r.status_code == 200
    assert "Jnotepad" in r.text
    assert "script-src 'self'" in r.headers["content-security-policy"]
