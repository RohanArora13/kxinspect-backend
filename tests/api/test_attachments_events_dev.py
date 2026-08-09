"""Attachment download, the event stream, dev controls and transport-level guards."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from app.api.v1.endpoints.events import _render
from app.core.config import MAX_ATTACHMENT_TOTAL_BYTES
from app.main import MAX_REQUEST_BYTES
from app.services.event_bus import EventBroker, replayable_events

from tests.api.test_commands import PNG, contest_files
from tests.conftest import DEV_TOKEN, Harness

SEED_PHOTO = "att_ph_chg001_01"
SEED_REPORT = "att_rpt_001"


class TestAttachmentDownload:
    def test_seed_photo_is_served_with_hardening_headers(self, harness: Harness) -> None:
        response = harness.client.get(f"/api/v1/attachments/{SEED_PHOTO}")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["accept-ranges"] == "bytes"
        assert response.headers["content-disposition"].startswith("inline;")

    def test_pdf_is_served_as_an_attachment(self, harness: Harness) -> None:
        response = harness.client.get(f"/api/v1/attachments/{SEED_REPORT}")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.headers["content-disposition"].startswith("attachment;")

    def test_unknown_opaque_id_is_404(self, harness: Harness) -> None:
        response = harness.client.get("/api/v1/attachments/att_missing")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "attachment.not_found"

    @pytest.mark.parametrize("opaque", ["..%2F..%2Fetc%2Fpasswd", "..", "%2e%2e"])
    def test_traversal_attempts_never_escape_the_media_roots(self, harness: Harness, opaque: str) -> None:
        response = harness.client.get(f"/api/v1/attachments/{opaque}")
        assert response.status_code in {404, 405}

    def test_a_satisfiable_range_returns_206(self, harness: Harness) -> None:
        full = harness.client.get(f"/api/v1/attachments/{SEED_PHOTO}").content
        response = harness.client.get(f"/api/v1/attachments/{SEED_PHOTO}", headers={"Range": "bytes=0-9"})
        assert response.status_code == 206
        assert response.content == full[:10]
        assert response.headers["content-range"] == f"bytes 0-9/{len(full)}"

    def test_a_suffix_range_returns_the_tail(self, harness: Harness) -> None:
        full = harness.client.get(f"/api/v1/attachments/{SEED_PHOTO}").content
        response = harness.client.get(f"/api/v1/attachments/{SEED_PHOTO}", headers={"Range": "bytes=-8"})
        assert response.status_code == 206
        assert response.content == full[-8:]

    def test_an_open_ended_range_runs_to_the_last_byte(self, harness: Harness) -> None:
        full = harness.client.get(f"/api/v1/attachments/{SEED_PHOTO}").content
        response = harness.client.get(f"/api/v1/attachments/{SEED_PHOTO}", headers={"Range": "bytes=10-"})
        assert response.status_code == 206
        assert response.content == full[10:]

    @pytest.mark.parametrize("header", ["bytes=999999999-", "bytes=20-5", "items=0-1", "bytes=-"])
    def test_unsatisfiable_ranges_are_416(self, harness: Harness, header: str) -> None:
        response = harness.client.get(f"/api/v1/attachments/{SEED_PHOTO}", headers={"Range": header})
        assert response.status_code == 416
        assert response.json()["error"]["code"] == "attachment.range_not_satisfiable"


class TestEventStream:
    """HTTP-level coverage of the paths whose response terminates on its own.

    A live subscription intentionally never ends, so the replay/fan-out behaviour is
    driven directly against the broker below rather than through a streaming client that
    would have to be abandoned mid-body.
    """

    def test_a_stale_cursor_yields_one_sync_required_and_closes(self, harness: Harness) -> None:
        events = read_events(harness, cursor="00000000-0000-4000-8000-000000000000:1:1")
        assert [event["event"] for event in events] == ["sync.required"]
        assert "id" not in events[0]

    def test_an_unparseable_cursor_yields_sync_required(self, harness: Harness) -> None:
        events = read_events(harness, cursor="garbage")
        assert [event["event"] for event in events] == ["sync.required"]

    def test_replayed_frames_carry_the_event_id_and_no_user_text(self, harness: Harness) -> None:
        harness.client.post(
            "/api/v1/charges/CHG-001/contest",
            files=contest_files(epoch=harness.state_epoch, version=1),
            headers=harness.command_headers(),
        )
        snapshot = asyncio.run(harness.store.snapshot())
        replay, servable = replayable_events(
            events=snapshot["events"],
            stream_epoch=snapshot["streamEpoch"],
            evicted_through=snapshot["streamEvictedThrough"],
            cursor=f"{snapshot['streamEpoch']}:0:0",
        )
        assert servable
        frames = [_render(event) for event in replay]
        # One commit publishes several ordered events: the boundary charge reconciles to
        # Accepted in the same transaction that records the contest.
        assert {frame["event"] for frame in frames} == {"charge.updated"}
        assert [frame["id"] for frame in frames] == [event["id"] for event in replay]

        contested = next(frame for frame in frames if json.loads(frame["data"])["entityId"] == "CHG-001")
        payload = json.loads(contested["data"])
        assert payload["data"] == {"resourceType": "charge", "changeType": "updated"}
        assert all("already loose" not in frame["data"] for frame in frames)

    def test_a_live_subscriber_receives_committed_events(self, harness: Harness) -> None:
        async def scenario() -> list[str]:
            subscriber = await harness.app.state.context.broker.register([])
            await harness.app.state.context.broker.publish(
                (
                    {
                        "id": "e:1:1",
                        "stateEpoch": "epoch",
                        "storeRevision": 1,
                        "type": "charge.updated",
                        "occurredAt": "2026-08-01T12:00:00Z",
                        "bookingId": "BKG-001",
                        "entityId": "CHG-001",
                        "entityVersion": 2,
                        "data": {"resourceType": "charge", "changeType": "updated"},
                    },
                )
            )
            event = await subscriber.next_event(timeout=1.0)
            assert event is not None
            return [event["id"]]

        assert asyncio.run(scenario()) == ["e:1:1"]

    def test_a_full_queue_closes_the_client_instead_of_blocking(self, harness: Harness) -> None:
        async def scenario() -> bool:
            broker = EventBroker(queue_capacity=1)
            subscriber = await broker.register([])
            event = {
                "id": "e:1:1",
                "stateEpoch": "epoch",
                "storeRevision": 1,
                "type": "charge.updated",
                "occurredAt": "2026-08-01T12:00:00Z",
                "bookingId": None,
                "entityId": "CHG-001",
                "entityVersion": 2,
                "data": {"resourceType": "charge", "changeType": "updated"},
            }
            await broker.publish((event, event, event))
            return subscriber.closed and broker.subscriber_count == 0

        assert asyncio.run(scenario())


def read_events(harness: Harness, *, cursor: str) -> list[dict[str, str]]:
    """Read every frame of a stream that ends by itself, then return the parsed frames."""
    collected: list[dict[str, str]] = []
    current: dict[str, str] = {}
    with harness.client.stream("GET", "/api/v1/events", headers={"Last-Event-ID": cursor}) as stream:
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        for raw in stream.iter_lines():
            line = raw.rstrip("\r")
            if line == "":
                if current:
                    collected.append(current)
                    current = {}
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            current[field] = value.lstrip()
    if current:
        collected.append(current)
    return collected


class TestDevRoutes:
    def test_dev_routes_are_absent_when_disabled(self, no_dev_harness: Harness) -> None:
        response = no_dev_harness.client.post("/api/v1/_dev/reset")
        assert response.status_code == 404
        assert no_dev_harness.client.get("/api/v1/health").json()["data"]["devRoutesEnabled"] is False

    def test_a_missing_dev_token_is_403(self, harness: Harness) -> None:
        response = harness.client.post("/api/v1/_dev/reset")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "dev.forbidden"

    def test_a_wrong_dev_token_is_403(self, harness: Harness) -> None:
        response = harness.client.post("/api/v1/_dev/reset", headers={"X-Dev-Token": "wrong"})
        assert response.status_code == 403

    def test_reset_restores_the_seed_and_rotates_the_epoch(
        self, harness: Harness, dev_headers: dict[str, str]
    ) -> None:
        harness.client.post(
            "/api/v1/charges/CHG-001/accept",
            json={"expectedStateEpoch": harness.state_epoch, "expectedVersion": 1},
            headers=harness.command_headers(),
        )
        assert harness.charge("CHG-001")["status"] == "accepted"
        before = harness.state_epoch

        response = harness.client.post("/api/v1/_dev/reset", headers=dev_headers)
        assert response.status_code == 204
        assert response.content == b""
        assert harness.state_epoch != before
        assert harness.charge("CHG-001")["status"] == "outstanding"

    def test_reset_deletes_uploaded_attachments(self, harness: Harness, dev_headers: dict[str, str]) -> None:
        harness.client.post(
            "/api/v1/charges/CHG-001/contest",
            files=contest_files(
                epoch=harness.state_epoch,
                version=1,
                attachments=[("evidence.png", PNG, "image/png")],
            ),
            headers=harness.command_headers(),
        )
        uploads = harness.settings.uploads_root
        assert any(path.suffix == ".png" for path in uploads.iterdir())
        harness.client.post("/api/v1/_dev/reset", headers=dev_headers)
        assert not any(path.suffix == ".png" for path in uploads.iterdir())

    def test_chaos_can_inject_a_deterministic_failure(
        self, harness: Harness, dev_headers: dict[str, str]
    ) -> None:
        response = harness.client.post(
            "/api/v1/_dev/chaos",
            json={"latencyMs": 0, "failNext": 1, "errorStatus": 503, "errorCode": "chaos.injected"},
            headers=dev_headers,
        )
        assert response.status_code == 200
        assert response.json()["data"]["failNext"] == 1

        failed = harness.client.get("/api/v1/bookings")
        assert failed.status_code == 503
        assert failed.json()["error"]["code"] == "chaos.injected"

        assert harness.client.get("/api/v1/bookings").status_code == 200

    def test_chaos_never_takes_health_down(self, harness: Harness, dev_headers: dict[str, str]) -> None:
        harness.client.post("/api/v1/_dev/chaos", json={"failNext": 5}, headers=dev_headers)
        assert harness.client.get("/api/v1/health").status_code == 200

    def test_raise_charge_adds_a_charge_and_a_notification(
        self, harness: Harness, dev_headers: dict[str, str]
    ) -> None:
        response = harness.client.post(
            "/api/v1/_dev/raise-charge",
            json={
                "id": "CHG-900",
                "bookingId": "BKG-001",
                "itemName": "Bathroom mirror",
                "type": "replace",
                "notes": "Cracked during the tenancy.",
                "location": "Oceanview > Apartment 2 > OVA111",
                "amountMinor": 4000,
                "currency": "GBP",
                "raisedAt": "2026-08-01T12:00:00Z",
                "gracePeriodDays": 30,
            },
            headers=dev_headers,
        )
        assert response.status_code == 201, response.text
        charge = response.json()["data"]
        assert charge["status"] == "outstanding"
        assert charge["deadlineAt"] == "2026-08-31T12:00:00Z"
        assert charge["version"] == 1

        feed = harness.client.get("/api/v1/notifications?unreadOnly=true").json()["data"]
        assert any(row["chargeId"] == "CHG-900" for row in feed)

    def test_raise_charge_rejects_a_duplicate_id(self, harness: Harness, dev_headers: dict[str, str]) -> None:
        payload: dict[str, Any] = {
            "id": "CHG-001",
            "bookingId": "BKG-001",
            "itemName": "Duplicate",
            "type": "repair",
            "location": "Oceanview > Apartment 2 > OVA111",
            "amountMinor": 100,
            "raisedAt": "2026-08-01T12:00:00Z",
        }
        response = harness.client.post("/api/v1/_dev/raise-charge", json=payload, headers=dev_headers)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "charge.duplicate_id"

    def test_operator_dismiss_resolves_a_contested_charge(
        self, harness: Harness, dev_headers: dict[str, str]
    ) -> None:
        response = harness.client.post(
            "/api/v1/_dev/resolve-charge",
            json={"chargeId": "CHG-003", "event": "operatorDismiss"},
            headers=dev_headers,
        )
        assert response.status_code == 200, response.text
        charge = response.json()["data"]
        assert charge["status"] == "resolved"
        assert charge["resolvedAt"] == "2026-08-01T12:00:00Z"
        assert charge["version"] == 3

    def test_operator_uphold_accepts_with_operator_origin(
        self, harness: Harness, dev_headers: dict[str, str]
    ) -> None:
        charge = harness.client.post(
            "/api/v1/_dev/resolve-charge",
            json={"chargeId": "CHG-003", "event": "operatorUphold"},
            headers=dev_headers,
        ).json()["data"]
        assert charge["status"] == "accepted"
        assert charge["acceptanceOrigin"] == "operator"

    def test_operator_decision_on_a_non_contested_charge_is_409(
        self, harness: Harness, dev_headers: dict[str, str]
    ) -> None:
        response = harness.client.post(
            "/api/v1/_dev/resolve-charge",
            json={"chargeId": "CHG-001", "event": "operatorDismiss"},
            headers=dev_headers,
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "charge.invalid_transition"


class TestTransportGuards:
    def test_a_declared_oversize_body_is_rejected_before_parsing(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/charges/CHG-001/contest",
            content=b"x",
            headers={
                **harness.command_headers(),
                "Content-Type": "multipart/form-data; boundary=x",
                "Content-Length": str(MAX_REQUEST_BYTES + 1),
            },
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "attachment.total_too_large"

    def test_the_body_cap_sits_above_the_documented_attachment_total(self) -> None:
        assert MAX_REQUEST_BYTES > MAX_ATTACHMENT_TOTAL_BYTES

    def test_a_configured_origin_is_echoed(self, harness: Harness) -> None:
        response = harness.client.get("/api/v1/bookings", headers={"Origin": "http://localhost:8080"})
        assert response.headers["access-control-allow-origin"] == "http://localhost:8080"
        assert "access-control-allow-credentials" not in response.headers

    def test_an_unlisted_origin_is_not_echoed(self, harness: Harness) -> None:
        response = harness.client.get("/api/v1/bookings", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in response.headers

    def test_preflight_allows_the_named_command_headers(self, harness: Harness) -> None:
        response = harness.client.options(
            "/api/v1/charges/CHG-001/accept",
            headers={
                "Origin": "http://localhost:8080",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,idempotency-key",
            },
        )
        assert response.status_code == 200
        allowed = response.headers["access-control-allow-headers"].lower()
        assert "idempotency-key" in allowed

    def test_the_dev_token_header_is_allowed_by_cors(self, harness: Harness) -> None:
        response = harness.client.options(
            "/api/v1/_dev/reset",
            headers={
                "Origin": "http://localhost:8080",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-dev-token",
            },
        )
        assert response.status_code == 200


def test_dev_token_constant_time_comparison_rejects_a_prefix(harness: Harness) -> None:
    response = harness.client.post("/api/v1/_dev/reset", headers={"X-Dev-Token": DEV_TOKEN[:-1]})
    assert response.status_code == 403
