"""Validated environment configuration.

``create_app()`` reads settings once; nothing else in the process consults ``os.environ``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Final

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.clock import parse_instant

SCHEMA_VERSION: Final[str] = "1"
CONTRACT_VERSION: Final[str] = "1"
API_MAJOR: Final[str] = "1"
SEED_VERSION: Final[str] = "2"

#: Frozen demo anchor. Fixture dates are authored relative to this instant so that
#: screenshot-era data cannot silently expire under a developer's wall clock.
REFERENCE_NOW: Final[str] = "2026-08-01T12:00:00Z"

DEFAULT_GRACE_PERIOD_DAYS: Final[int] = 30
EVENT_RING_CAPACITY: Final[int] = 100

MAX_ATTACHMENTS: Final[int] = 5
MAX_ATTACHMENT_BYTES: Final[int] = 10 * 1024 * 1024
MAX_ATTACHMENT_TOTAL_BYTES: Final[int] = 25 * 1024 * 1024
CONTEST_REASON_MIN_GRAPHEMES: Final[int] = 10
CONTEST_REASON_MAX_GRAPHEMES: Final[int] = 2000

ALLOWED_ATTACHMENT_MEDIA_TYPES: Final[dict[str, tuple[str, ...]]] = {
    "image/jpeg": (".jpg", ".jpeg"),
    "image/png": (".png",),
    "image/webp": (".webp",),
    "video/mp4": (".mp4",),
    "application/pdf": (".pdf",),
}


class Settings(BaseSettings):
    """Process configuration. Every field is validated before the app is constructed."""

    model_config = SettingsConfigDict(
        env_prefix="KX_",
        env_file=None,
        extra="forbid",
        frozen=True,
    )

    host: str = "127.0.0.1"
    port: int = Field(default=8181, ge=1, le=65535)
    workers: int = Field(default=1, ge=1)

    runtime_root: Path = Path("runtime")
    seed_root: Path = Path("app/seed")
    static_root: Path = Path("app/static/photos")

    enable_dev_routes: bool = False
    dev_token: str = ""

    cors_origins: tuple[str, ...] = ("http://localhost:8080", "http://127.0.0.1:8080")

    grace_period_days: int = Field(default=DEFAULT_GRACE_PERIOD_DAYS, ge=0, le=3650)

    #: When set, the service runs on an anchored demo clock instead of the wall clock.
    demo_now: str | None = None

    sse_heartbeat_seconds: float = Field(default=15.0, gt=0)
    sse_queue_capacity: int = Field(default=64, ge=1)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("cors_origins")
    @classmethod
    def _reject_wildcard(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if "*" in value:
            raise ValueError("wildcard CORS origin is rejected; name explicit origins")
        return value

    @field_validator("demo_now")
    @classmethod
    def _validate_demo_now(cls, value: str | None) -> str | None:
        if value is not None:
            parse_instant(value)
        return value

    @model_validator(mode="after")
    def _validate_combination(self) -> Settings:
        if self.workers != 1:
            raise ValueError(
                "the JSON store supports exactly one Uvicorn worker; "
                "a process-local lock is not a multi-process database"
            )
        if self.enable_dev_routes and not self.dev_token:
            raise ValueError("KX_DEV_TOKEN must be a non-empty value when dev routes are enabled")
        return self

    @property
    def demo_instant(self) -> datetime | None:
        return parse_instant(self.demo_now) if self.demo_now else None

    @property
    def state_path(self) -> Path:
        return self.runtime_root / "state.json"

    @property
    def uploads_root(self) -> Path:
        return self.runtime_root / "uploads"

    @property
    def staging_root(self) -> Path:
        return self.uploads_root / ".staging"

    @property
    def lock_path(self) -> Path:
        return self.runtime_root / "runtime.lock"

    @property
    def marker_path(self) -> Path:
        return self.runtime_root / ".kxinspect-runtime"


#: Marker payload that ``reset_runtime.py`` must find before deleting anything.
RUNTIME_MARKER_UUID: Final[str] = "6f2a1e34-5b7c-4d18-9e0a-2c7d5b8f4a13"
