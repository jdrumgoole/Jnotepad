"""Captured items: notes, links and images, all owned by a single user."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from bson import Binary, ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from pydantic import AfterValidator, BaseModel, Field
from regstack import RegStack

Kind = Literal["note", "link", "image"]

MAX_TAGS = 20
MAX_IMAGES_PER_ITEM = 20

# Sniff the real type from magic bytes; never trust the client's Content-Type.
# SVG is deliberately excluded: it can carry script.
_SIGNATURES: list[tuple[bytes, int, str]] = [
    (b"\x89PNG\r\n\x1a\n", 0, "image/png"),
    (b"\xff\xd8\xff", 0, "image/jpeg"),
    (b"GIF87a", 0, "image/gif"),
    (b"GIF89a", 0, "image/gif"),
]


def sniff_image_type(data: bytes) -> str | None:
    for sig, offset, mime in _SIGNATURES:
        if data[offset:offset + len(sig)] == sig:
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _check_url(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    # Only http(s): a stored ``javascript:`` URL would be an XSS vector when clicked.
    if not re.match(r"^https?://[^\s/$.?#][^\s]*$", value, re.IGNORECASE):
        raise ValueError("URL must start with http:// or https://")
    return value


def _clean_tags(tags: list[str]) -> list[str]:
    out: list[str] = []
    for t in tags:
        t = t.strip().lstrip("#").lower()
        if t and t not in out:
            if len(t) > 40:
                raise ValueError("Tags must be at most 40 characters")
            out.append(t)
    if len(out) > MAX_TAGS:
        raise ValueError(f"At most {MAX_TAGS} tags")
    return out


Url = Annotated[str | None, Field(default=None, max_length=2048), AfterValidator(_check_url)]
Tags = Annotated[list[str], AfterValidator(_clean_tags)]


class ItemCreate(BaseModel):
    title: str = Field(default="", max_length=300)
    text: str = Field(default="", max_length=200_000)
    url: Url = None
    tags: Tags = Field(default_factory=list)


class ItemUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    text: str | None = Field(default=None, max_length=200_000)
    url: Url = None
    tags: Tags | None = None
    """Fields left out are unchanged. Send ``"url": null`` to clear the URL."""


class ImageMeta(BaseModel):
    id: str
    content_type: str
    size: int
    filename: str


class ItemOut(BaseModel):
    id: str
    kind: Kind
    title: str
    text: str
    url: str | None
    tags: list[str]
    images: list[ImageMeta]
    created_at: datetime
    updated_at: datetime


class ItemPage(BaseModel):
    items: list[ItemOut]
    total: int


def _kind(doc: dict[str, Any]) -> Kind:
    if doc.get("images"):
        return "image"
    if doc.get("url"):
        return "link"
    return "note"


def _out(doc: dict[str, Any]) -> ItemOut:
    return ItemOut(
        id=str(doc["_id"]),
        kind=_kind(doc),
        title=doc.get("title", ""),
        text=doc.get("text", ""),
        url=doc.get("url"),
        tags=doc.get("tags", []),
        images=[ImageMeta(**m) for m in doc.get("images", [])],
        created_at=doc["created_at"],
        updated_at=doc["updated_at"],
    )


def _oid(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        raise HTTPException(404, "Not found") from None


def _now() -> datetime:
    # Mongo stores milliseconds; truncate so responses match what is read back.
    now = datetime.now(UTC)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def build_items_router(rs: RegStack) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["items"])
    current_user = rs.deps.current_user()

    def db(request: Request):
        return request.app.state.db

    async def owned(db, owner: str, item_id: str) -> dict[str, Any]:
        doc = await db.items.find_one({"_id": _oid(item_id), "owner_id": owner})
        if doc is None:
            raise HTTPException(404, "Not found")
        return doc

    @router.get("/items", response_model=ItemPage)
    async def list_items(
        q: str | None = Query(default=None, max_length=200),
        tag: str | None = Query(default=None, max_length=40),
        kind: Kind | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        user=Depends(current_user), db=Depends(db),
    ):
        flt: dict[str, Any] = {"owner_id": str(user.id)}
        if tag:
            flt["tags"] = tag.strip().lstrip("#").lower()
        if kind == "image":
            flt["images.0"] = {"$exists": True}
        elif kind == "link":
            flt["images.0"] = {"$exists": False}
            flt["url"] = {"$type": "string"}
        elif kind == "note":
            flt["images.0"] = {"$exists": False}
            flt["url"] = None
        if q and q.strip():
            # Escape: user input must never be interpreted as a regex (ReDoS).
            rx = {"$regex": re.escape(q.strip()), "$options": "i"}
            flt["$or"] = [{"title": rx}, {"text": rx}, {"url": rx}, {"tags": rx}]
        total = await db.items.count_documents(flt)
        cursor = db.items.find(flt).sort([("updated_at", -1), ("_id", -1)]).skip(offset).limit(
            limit)
        return ItemPage(items=[_out(d) async for d in cursor], total=total)

    @router.post("/items", response_model=ItemOut, status_code=201)
    async def create_item(body: ItemCreate, user=Depends(current_user), db=Depends(db)):
        now = _now()
        doc = {**body.model_dump(), "owner_id": str(user.id), "images": [],
               "created_at": now, "updated_at": now}
        res = await db.items.insert_one(doc)
        doc["_id"] = res.inserted_id
        return _out(doc)

    @router.get("/items/{item_id}", response_model=ItemOut)
    async def get_item(item_id: str, user=Depends(current_user), db=Depends(db)):
        return _out(await owned(db, str(user.id), item_id))

    @router.patch("/items/{item_id}", response_model=ItemOut)
    async def update_item(item_id: str, body: ItemUpdate, user=Depends(current_user),
                          db=Depends(db)):
        owner = str(user.id)
        await owned(db, owner, item_id)
        changes = body.model_dump(exclude_unset=True)
        changes = {k: v for k, v in changes.items() if v is not None or k == "url"}
        changes["updated_at"] = _now()
        await db.items.update_one({"_id": _oid(item_id), "owner_id": owner}, {"$set": changes})
        return _out(await owned(db, owner, item_id))

    @router.delete("/items/{item_id}", status_code=204)
    async def delete_item(item_id: str, user=Depends(current_user), db=Depends(db)):
        owner = str(user.id)
        res = await db.items.delete_one({"_id": _oid(item_id), "owner_id": owner})
        if res.deleted_count == 0:
            raise HTTPException(404, "Not found")
        await db.images.delete_many({"owner_id": owner, "item_id": item_id})
        return Response(status_code=204)

    @router.post("/items/{item_id}/images", response_model=ItemOut, status_code=201)
    async def add_image(item_id: str, request: Request, file: UploadFile = File(...),
                        user=Depends(current_user), db=Depends(db)):
        owner = str(user.id)
        doc = await owned(db, owner, item_id)
        if len(doc.get("images", [])) >= MAX_IMAGES_PER_ITEM:
            raise HTTPException(400, f"At most {MAX_IMAGES_PER_ITEM} images per item")
        limit = request.app.state.settings.max_image_bytes
        data = await file.read(limit + 1)
        if len(data) > limit:
            raise HTTPException(413, f"Image larger than {limit // (1024 * 1024)} MiB")
        mime = sniff_image_type(data)
        if mime is None:
            raise HTTPException(415, "Only PNG, JPEG, GIF and WebP images are supported")
        filename = (file.filename or "image")[:200]
        image_id = ObjectId()
        await db.images.insert_one({
            "_id": image_id, "owner_id": owner, "item_id": item_id,
            "content_type": mime, "size": len(data), "filename": filename,
            "data": Binary(data), "created_at": _now(),
        })
        meta = {"id": str(image_id), "content_type": mime, "size": len(data),
                "filename": filename}
        await db.items.update_one({"_id": doc["_id"], "owner_id": owner},
                                  {"$push": {"images": meta}, "$set": {"updated_at": _now()}})
        return _out(await owned(db, owner, item_id))

    @router.delete("/items/{item_id}/images/{image_id}", response_model=ItemOut)
    async def remove_image(item_id: str, image_id: str, user=Depends(current_user),
                           db=Depends(db)):
        owner = str(user.id)
        doc = await owned(db, owner, item_id)
        res = await db.images.delete_one({"_id": _oid(image_id), "owner_id": owner,
                                          "item_id": item_id})
        if res.deleted_count == 0:
            raise HTTPException(404, "Not found")
        await db.items.update_one({"_id": doc["_id"], "owner_id": owner},
                                  {"$pull": {"images": {"id": image_id}},
                                   "$set": {"updated_at": _now()}})
        return _out(await owned(db, owner, item_id))

    @router.get("/images/{image_id}")
    async def get_image(image_id: str, user=Depends(current_user), db=Depends(db)):
        img = await db.images.find_one({"_id": _oid(image_id), "owner_id": str(user.id)})
        if img is None:
            raise HTTPException(404, "Not found")
        return Response(
            content=bytes(img["data"]), media_type=img["content_type"],
            headers={"Cache-Control": "private, max-age=86400",
                     "Content-Disposition": "inline"},
        )

    @router.get("/tags", response_model=list[str])
    async def list_tags(user=Depends(current_user), db=Depends(db)):
        return sorted(await db.items.distinct("tags", {"owner_id": str(user.id)}))

    return router
