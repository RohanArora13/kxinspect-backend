"""Read endpoints: envelopes, projections, filters and typed not-found behaviour."""

from __future__ import annotations

from typing import Any

import pytest
from app.core.config import REFERENCE_NOW

from tests.conftest import Harness

META_KEYS = {"schemaVersion", "requestId", "serverTime", "stateEpoch", "storeRevision"}


def meta(payload: dict[str, Any]) -> dict[str, Any]:
    return payload["meta"]


class TestEnvelope:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/health",
            "/api/v1/bookings",
            "/api/v1/bookings/BKG-001/hub",
            "/api/v1/inventory-reports/RPT-001",
            "/api/v1/inspections/INS-001",
            "/api/v1/charges",
            "/api/v1/charges/CHG-001",
            "/api/v1/notifications",
        ],
    )
    def test_every_json_success_carries_data_and_meta(self, harness: Harness, path: str) -> None:
        response = harness.client.get(path)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert set(payload) == {"data", "meta"}
        assert set(meta(payload)) >= META_KEYS
        assert meta(payload)["schemaVersion"] == "1"
        assert meta(payload)["serverTime"] == REFERENCE_NOW

    def test_sync_snapshot_adds_stream_identity(self, harness: Harness) -> None:
        payload = harness.client.get("/api/v1/sync-snapshot").json()
        assert META_KEYS | {"streamEpoch", "streamCursor"} == set(meta(payload))
        assert meta(payload)["streamCursor"].startswith(meta(payload)["streamEpoch"])

    def test_errors_carry_the_same_meta(self, harness: Harness) -> None:
        response = harness.client.get("/api/v1/charges/NOPE")
        assert response.status_code == 404
        payload = response.json()
        assert set(payload) == {"error", "meta"}
        assert payload["error"]["code"] == "charge.not_found"
        assert payload["error"]["details"]["chargeId"] == "NOPE"
        assert set(meta(payload)) >= META_KEYS

    def test_request_ids_are_unique_per_request(self, harness: Harness) -> None:
        first = meta(harness.client.get("/api/v1/bookings").json())["requestId"]
        second = meta(harness.client.get("/api/v1/bookings").json())["requestId"]
        assert first != second


class TestHub:
    def test_hub_projects_only_the_requested_booking(self, harness: Harness) -> None:
        data = harness.client.get("/api/v1/bookings/BKG-001/hub").json()["data"]
        assert data["booking"]["id"] == "BKG-001"
        assert {row["bookingId"] for row in data["charges"]} == {"BKG-001"}
        assert {row["bookingId"] for row in data["inspections"]} == {"BKG-001"}
        assert data["generatedAt"] == REFERENCE_NOW

    def test_empty_hub_booking_returns_empty_collections_not_an_error(self, harness: Harness) -> None:
        data = harness.client.get("/api/v1/bookings/BKG-003/hub").json()["data"]
        assert data["booking"]["id"] == "BKG-003"
        assert data["charges"] == []
        assert data["tasks"] == []
        assert data["inspections"] == []
        assert data["inventoryReports"] == []

    def test_unknown_booking_is_404(self, harness: Harness) -> None:
        response = harness.client.get("/api/v1/bookings/BKG-999/hub")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "booking.not_found"

    def test_hub_notifications_are_scoped_to_that_booking_s_charges(self, harness: Harness) -> None:
        data = harness.client.get("/api/v1/bookings/BKG-001/hub").json()["data"]
        charge_ids = {row["id"] for row in data["charges"]}
        assert all(row["chargeId"] in charge_ids for row in data["notifications"])


class TestDeadlineReconciliationOnRead:
    def test_the_boundary_charge_is_accepted_at_reference_now(self, harness: Harness) -> None:
        charge = harness.charge("CHG-006")
        assert charge["status"] == "accepted"
        assert charge["acceptedAt"] == "2026-08-01T12:00:00Z"
        assert charge["acceptanceOrigin"] == "deadline"
        assert charge["version"] == 2

    def test_a_future_deadline_is_left_outstanding(self, harness: Harness) -> None:
        charge = harness.charge("CHG-001")
        assert charge["status"] == "outstanding"
        assert charge["version"] == 1

    def test_reconciliation_runs_exactly_once_per_charge(self, harness: Harness) -> None:
        first = harness.charge("CHG-006")
        revision = harness.store.store_revision
        second = harness.charge("CHG-006")
        assert first["version"] == second["version"]
        assert harness.store.store_revision == revision


class TestChargeFilters:
    def test_booking_filter(self, harness: Harness) -> None:
        rows = harness.client.get("/api/v1/charges?bookingId=BKG-002").json()["data"]
        assert {row["bookingId"] for row in rows} == {"BKG-002"}

    def test_repeated_status_filter(self, harness: Harness) -> None:
        rows = harness.client.get("/api/v1/charges?status=paid&status=resolved").json()["data"]
        assert {row["status"] for row in rows} == {"paid", "resolved"}

    def test_unknown_status_is_422(self, harness: Harness) -> None:
        response = harness.client.get("/api/v1/charges?status=archived")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation.failed"
        assert response.json()["error"]["details"]["status"] == ["archived"]


class TestNotifications:
    def test_unread_only(self, harness: Harness) -> None:
        rows = harness.client.get("/api/v1/notifications?unreadOnly=true").json()["data"]
        assert rows
        assert all(row["read"] is False for row in rows)

    def test_after_is_exclusive(self, harness: Harness) -> None:
        rows = harness.client.get("/api/v1/notifications?after=2026-07-15T09:00:05Z").json()["data"]
        assert "NTF-001" not in {row["id"] for row in rows}
        assert "NTF-002" in {row["id"] for row in rows}

    def test_malformed_after_is_422(self, harness: Harness) -> None:
        response = harness.client.get("/api/v1/notifications?after=yesterday")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation.failed"


class TestNotFound:
    @pytest.mark.parametrize(
        ("path", "code"),
        [
            ("/api/v1/inventory-reports/RPT-404", "report.not_found"),
            ("/api/v1/inspections/INS-404", "inspection.not_found"),
            ("/api/v1/charges/CHG-404", "charge.not_found"),
        ],
    )
    def test_unknown_ids_map_to_typed_codes(self, harness: Harness, path: str, code: str) -> None:
        response = harness.client.get(path)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == code
