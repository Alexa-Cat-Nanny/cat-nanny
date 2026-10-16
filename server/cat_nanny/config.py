"""All runtime settings, read from environment variables (see .env.example).

Locally everything defaults to in-memory storage and photos on disk, so the
server runs with no AWS account. On Lambda the SAM template sets the AWS values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _list(name: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


@dataclass(frozen=True)
class Settings:
    store: str = "memory"
    """memory | dynamodb"""
    table_name: str = ""
    photos: str = "local"
    """local | s3"""
    photo_bucket: str = ""
    photo_dir: str = "uploads"
    """Local photo files (photos=local)."""
    public_base_url: str = "http://127.0.0.1:8000"
    """Where phones reach this server (upload page and local photo files)."""
    household_id: str = "demo"
    """Alexa+ passes no user or household id to the server, so the demo serves one household.
    Account linking (OAuth) is what would identify the household in production."""
    household_tz: str = "America/Los_Angeles"
    auth_token: str = ""
    """If set, every /mcp request must send `Authorization: Bearer <token>`."""
    allowed_origins: list[str] = field(default_factory=list)
    """Browser origins allowed to call /mcp. Requests without an Origin header (server-to-server) are allowed."""
    seed_on_start: bool = False
    """Memory store only: load the demo history at startup."""

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.household_tz)

    @property
    def photo_root(self) -> Path:
        return Path(self.photo_dir)


@lru_cache
def get_settings() -> Settings:
    return Settings(
        store=_env("STORE", "memory"),
        table_name=_env("TABLE_NAME", ""),
        photos=_env("PHOTOS", "local"),
        photo_bucket=_env("PHOTO_BUCKET", ""),
        photo_dir=_env("PHOTO_DIR", "uploads"),
        public_base_url=_env("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
        household_id=_env("HOUSEHOLD_ID", "demo"),
        household_tz=_env("HOUSEHOLD_TZ", "America/Los_Angeles"),
        auth_token=_env("MCP_AUTH_TOKEN", ""),
        allowed_origins=_list("ALLOWED_ORIGINS"),
        seed_on_start=_env("SEED_ON_START", "").lower() in ("1", "true", "yes"),
    )
