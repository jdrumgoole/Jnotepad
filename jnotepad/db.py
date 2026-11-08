"""Database lifecycle: embedded SecantusDB server, or an external MongoDB-wire URL."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from jnotepad.config import Settings

log = logging.getLogger(__name__)


@contextmanager
def database_server(settings: Settings) -> Iterator[str]:
    """Yield a ``mongodb://`` base URI (no database path).

    With ``JNOTEPAD_MONGODB_URL`` set, that URL is used as-is and nothing is
    started. Otherwise the pure-Python SecantusDB server runs in-process on an
    OS-assigned loopback port, backed by WiredTiger files under ``data_dir``.
    """
    if settings.mongodb_url:
        yield settings.mongodb_url.rstrip("/")
        return

    from secantus import SecantusDBServer  # imported lazily: heavy, optional path

    settings.secantus_path.mkdir(parents=True, exist_ok=True)
    with SecantusDBServer(storage_path=str(settings.secantus_path)) as server:
        log.info("Embedded SecantusDB listening on %s (store: %s)", server.uri,
                 settings.secantus_path)
        yield server.uri.rstrip("/")


async def install_indexes(db) -> None:
    await db.items.create_index([("owner_id", 1), ("updated_at", -1)])
    await db.items.create_index([("owner_id", 1), ("tags", 1)])
    await db.images.create_index([("owner_id", 1), ("item_id", 1)])
