"""Application settings, loaded from ``JNOTEPAD_*`` environment variables."""

from __future__ import annotations

import os
import secrets
from functools import cached_property
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# regstack identifies users by email. Jnotepad users log in with a username,
# which is mapped to ``<username>@<USERNAME_EMAIL_DOMAIN>``. ``.internal`` is
# reserved by ICANN for private use, so these addresses can never be routable,
# yet they still pass pydantic's EmailStr validation (``.local``/``.test`` don't).
USERNAME_EMAIL_DOMAIN = "users.jnotepad.internal"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="JNOTEPAD_", extra="ignore")

    data_dir: Path = Path("./data")
    """Holds the embedded SecantusDB store and the generated JWT secret."""

    mongodb_url: str | None = None
    """Connect to an already-running MongoDB-wire server (e.g. ``secantusd-rs``)
    instead of starting the embedded Python SecantusDB server."""

    database: str = "jnotepad"

    jwt_secret: SecretStr | None = None
    """If unset, a secret is generated once and persisted to ``data_dir``."""

    jwt_ttl_seconds: int = 60 * 60 * 24 * 7

    allow_registration: bool = True

    max_image_bytes: int = Field(default=8 * 1024 * 1024, le=15 * 1024 * 1024)
    """Images are stored inline in a BSON document, so this must stay well
    under MongoDB's 16 MiB document limit."""

    base_url: str = "http://localhost:8000"

    @property
    def secantus_path(self) -> Path:
        return self.data_dir / "secantus"

    @cached_property
    def resolved_jwt_secret(self) -> str:
        if self.jwt_secret is not None:
            return self.jwt_secret.get_secret_value()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / "jwt_secret"
        if path.exists():
            return path.read_text().strip()
        value = secrets.token_urlsafe(64)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(value)
        return value
