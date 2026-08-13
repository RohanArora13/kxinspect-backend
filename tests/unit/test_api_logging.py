"""Terminal API access logging."""

from __future__ import annotations

import logging

import pytest

from tests.conftest import Harness


def test_completed_api_call_is_logged(harness: Harness, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="uvicorn.error")

    response = harness.client.get("/api/v1/bookings")

    assert response.status_code == 200
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        message.startswith("api call method=GET path=/api/v1/bookings status=200 duration_ms=")
        and "request_id=" in message
        for message in messages
    )


def test_cors_preflight_is_logged(harness: Harness, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="uvicorn.error")

    response = harness.client.options(
        "/api/v1/bookings",
        headers={
            "Origin": "http://localhost:8080",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        message.startswith("api call method=OPTIONS path=/api/v1/bookings status=200 duration_ms=")
        and not message.endswith("request_id=")
        for message in messages
    )
