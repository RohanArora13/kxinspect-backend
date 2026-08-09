"""Shared test wiring.

Every test builds its own app with a temporary runtime root, a :class:`ManualClock`
anchored at ``referenceNow``, a manual scheduler and sequential ids. Nothing writes to the
repository's ``runtime/`` directory and no test depends on wall-clock time.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from app.core.clock import ManualClock, ManualScheduler, parse_instant
from app.core.config import REFERENCE_NOW, Settings
from app.core.ids import SequenceIdGenerator
from app.db.store import JsonStore
from app.main import build_app
from fastapi import FastAPI
from fastapi.testclient import TestClient

DEV_TOKEN = "test-dev-token"


@dataclass(slots=True)
class Harness:
    app: FastAPI
    client: TestClient
    settings: Settings
    store: JsonStore
    clock: ManualClock
    scheduler: ManualScheduler

    @property
    def state_epoch(self) -> str:
        return self.store.state_epoch

    def charge(self, charge_id: str) -> dict[str, object]:
        response = self.client.get(f"/api/v1/charges/{charge_id}")
        assert response.status_code == 200, response.text
        data: dict[str, object] = response.json()["data"]
        return data

    def command_headers(self, key: str | None = None) -> dict[str, str]:
        return {"Idempotency-Key": key or str(uuid.uuid4())}


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    repo_root = Path(__file__).resolve().parent.parent
    defaults: dict[str, object] = {
        "runtime_root": tmp_path / "runtime",
        "seed_root": repo_root / "app" / "seed",
        "static_root": repo_root / "app" / "static" / "photos",
        "enable_dev_routes": True,
        "dev_token": DEV_TOKEN,
        # Short heartbeat so a stream notices a disconnected test client promptly.
        "sse_heartbeat_seconds": 0.2,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def build_harness(settings: Settings, *, now: str = REFERENCE_NOW) -> tuple[FastAPI, Harness]:
    clock = ManualClock(parse_instant(now))
    scheduler = ManualScheduler()
    store = JsonStore(settings, clock)
    app = build_app(settings, store, clock, scheduler, SequenceIdGenerator())
    client = TestClient(app)
    return app, Harness(
        app=app,
        client=client,
        settings=settings,
        store=store,
        clock=clock,
        scheduler=scheduler,
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def harness(settings: Settings) -> Iterator[Harness]:
    _, built = build_harness(settings)
    with built.client:
        yield built


@pytest.fixture
def no_dev_harness(tmp_path: Path) -> Iterator[Harness]:
    configured = make_settings(tmp_path, enable_dev_routes=False, dev_token="")
    _, built = build_harness(configured)
    with built.client:
        yield built


@pytest.fixture
def dev_headers() -> dict[str, str]:
    return {"X-Dev-Token": DEV_TOKEN}
