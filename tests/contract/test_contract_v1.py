"""Contract v1 freeze tests.

These are the tests that make the contract a contract rather than a description: the
OpenAPI snapshot, the fixture bundle, the error catalogue and the golden request/response
examples all have to agree with what the running service actually does.

Regenerate the golden examples deliberately with::

    KX_UPDATE_EXAMPLES=1 uv run pytest tests/contract -q
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from app.core.canonical_json import canonical_json
from app.core.errors import ERROR_CATALOG, ApiError
from app.domain.charge_state import ChargeEvent, ChargeStatus
from scripts.export_fixtures import export
from scripts.export_openapi import generate, normalize
from scripts.verify_fixture_manifest import verify

from tests.api.test_commands import PNG
from tests.conftest import Harness

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EXAMPLES_DIR = REPO_ROOT / "docs" / "contracts" / "examples"
FRONTEND_BUNDLE = REPO_ROOT.parent / "kxinspect_frontend_flutter" / "assets" / "fixtures"

UPDATE = os.environ.get("KX_UPDATE_EXAMPLES") == "1"

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_ATTACHMENT_ID = re.compile(r"att_[0-9a-f]{32}")


def stabilise(value: Any) -> Any:
    """Replace values that legitimately differ per run with named placeholders."""
    if isinstance(value, dict):
        return {key: stabilise(item) for key, item in value.items()}
    if isinstance(value, list):
        return [stabilise(item) for item in value]
    if isinstance(value, str):
        replaced = _ATTACHMENT_ID.sub("att_<generated>", value)
        return _UUID.sub("<uuid>", replaced)
    return value


def golden(name: str, payload: Any) -> None:
    """Compare ``payload`` with the committed example, or rewrite it when asked."""
    path = EXAMPLES_DIR / f"{name}.json"
    encoded = canonical_json(stabilise(payload)) + b"\n"
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded)
        return
    assert path.is_file(), f"missing golden example {path}; regenerate with KX_UPDATE_EXAMPLES=1"
    committed = path.read_bytes()
    assert committed == encoded, (
        f"{path.name} drifted from the live response. "
        "If the change is intended, this is a G-01 contract change."
    )


class TestOpenApiSnapshot:
    def test_the_frozen_snapshot_matches_the_running_app(self) -> None:
        frozen = (REPO_ROOT / "docs" / "openapi-v1.json").read_bytes()
        assert frozen == normalize(generate())

    def test_every_documented_endpoint_is_present(self) -> None:
        document = generate()
        expected = {
            "/api/v1/health",
            "/api/v1/bookings",
            "/api/v1/bookings/{booking_id}/hub",
            "/api/v1/sync-snapshot",
            "/api/v1/inventory-reports/{report_id}",
            "/api/v1/inspections/{inspection_id}",
            "/api/v1/charges",
            "/api/v1/charges/{charge_id}",
            "/api/v1/attachments/{opaque_id}",
            "/api/v1/charges/{charge_id}/accept",
            "/api/v1/charges/{charge_id}/contest",
            "/api/v1/charges/{charge_id}/pay",
            "/api/v1/tasks",
            "/api/v1/notifications",
            "/api/v1/notifications/{notification_id}/read",
            "/api/v1/events",
            "/api/v1/_dev/reset",
            "/api/v1/_dev/chaos",
            "/api/v1/_dev/raise-charge",
            "/api/v1/_dev/resolve-charge",
        }
        assert set(document["paths"]) == expected


class TestErrorCatalog:
    def test_every_catalog_code_is_unique(self) -> None:
        codes = [code for code, _, _ in ERROR_CATALOG]
        assert len(codes) == len(set(codes))

    def test_every_raisable_error_class_is_in_the_catalog(self) -> None:
        catalog = {code: status for code, status, _ in ERROR_CATALOG}
        missing: list[str] = []
        for subclass in _all_subclasses(ApiError):
            if subclass.code not in catalog:
                missing.append(f"{subclass.__name__}({subclass.code})")
            elif catalog[subclass.code] != subclass.status_code:
                missing.append(f"{subclass.code} status {subclass.status_code}")
        assert not missing, f"error catalogue is out of date: {missing}"

    def test_statuses_are_within_the_documented_set(self) -> None:
        allowed = {400, 403, 404, 409, 413, 415, 416, 422, 500, 503}
        assert {status for _, status, _ in ERROR_CATALOG} <= allowed


def _all_subclasses(root: type[ApiError]) -> list[type[ApiError]]:
    found: list[type[ApiError]] = []
    for subclass in root.__subclasses__():
        found.append(subclass)
        found.extend(_all_subclasses(subclass))
    return found


class TestFixtureBundle:
    def test_a_freshly_exported_bundle_verifies(self, tmp_path: Path) -> None:
        export(tmp_path / "bundle")
        assert verify(tmp_path / "bundle") == []

    def test_export_is_byte_stable_across_runs(self, tmp_path: Path) -> None:
        first = export(tmp_path / "a")
        second = export(tmp_path / "b")
        assert first == second
        for entry in first["files"]:
            assert (tmp_path / "a" / entry["path"]).read_bytes() == (
                tmp_path / "b" / entry["path"]
            ).read_bytes()

    def test_the_manifest_excludes_itself(self, tmp_path: Path) -> None:
        manifest = export(tmp_path / "bundle")
        assert "manifest.json" not in {entry["path"] for entry in manifest["files"]}

    def test_coverage_claims_are_backed_by_the_data(self, tmp_path: Path) -> None:
        manifest = export(tmp_path / "bundle")
        coverage = manifest["coverage"]
        assert set(coverage["statuses"]) == {item.value for item in ChargeStatus}
        assert set(coverage["acceptanceOrigins"]) == {"student", "deadline", "operator"}
        assert coverage["deadlineBoundaryCharges"]
        assert coverage["emptyHubBookings"]
        assert len(coverage["currencies"]) >= 2

    def test_the_state_vectors_cover_every_status_event_pair(self, tmp_path: Path) -> None:
        export(tmp_path / "bundle")
        vectors = json.loads(
            (tmp_path / "bundle" / "vectors" / "charge_state_vectors.json").read_text("utf-8")
        )
        pairs = {(vector["from"], vector["event"]) for vector in vectors}
        assert pairs == {(status.value, event.value) for status in ChargeStatus for event in ChargeEvent}

    @pytest.mark.skipif(
        not FRONTEND_BUNDLE.is_dir(), reason="frontend checkout is not present next to this repo"
    )
    def test_the_frontend_copy_matches_a_fresh_export(self, tmp_path: Path) -> None:
        """Cross-repo drift check. Ordinary CI verifies only its own repository."""
        fresh = export(tmp_path / "bundle")
        committed = json.loads((FRONTEND_BUNDLE / "manifest.json").read_text("utf-8"))
        assert committed["bundleDigest"] == fresh["bundleDigest"]
        assert verify(FRONTEND_BUNDLE) == []


class TestGoldenExamples:
    """Exact request/response bodies a client implementer can code against."""

    def test_health(self, harness: Harness) -> None:
        golden("get_health_200", harness.client.get("/api/v1/health").json())

    def test_hub(self, harness: Harness) -> None:
        golden(
            "get_booking_hub_200",
            harness.client.get("/api/v1/bookings/BKG-001/hub").json(),
        )

    def test_empty_hub(self, harness: Harness) -> None:
        golden(
            "get_booking_hub_empty_200",
            harness.client.get("/api/v1/bookings/BKG-003/hub").json(),
        )

    def test_charge_detail(self, harness: Harness) -> None:
        golden("get_charge_200", harness.client.get("/api/v1/charges/CHG-001").json())

    def test_inspection_detail(self, harness: Harness) -> None:
        golden("get_inspection_200", harness.client.get("/api/v1/inspections/INS-001").json())

    def test_inventory_report(self, harness: Harness) -> None:
        golden(
            "get_inventory_report_200",
            harness.client.get("/api/v1/inventory-reports/RPT-001").json(),
        )

    def test_notifications(self, harness: Harness) -> None:
        golden("get_notifications_200", harness.client.get("/api/v1/notifications").json())

    def test_sync_snapshot_meta(self, harness: Harness) -> None:
        payload = harness.client.get("/api/v1/sync-snapshot").json()
        golden("get_sync_snapshot_meta_200", {"meta": payload["meta"]})

    def test_accept(self, harness: Harness) -> None:
        request = {"expectedStateEpoch": harness.state_epoch, "expectedVersion": 1}
        response = harness.client.post(
            "/api/v1/charges/CHG-001/accept", json=request, headers=harness.command_headers()
        )
        golden(
            "post_charge_accept_200",
            {"request": request, "status": response.status_code, "response": response.json()},
        )

    def test_contest(self, harness: Harness) -> None:
        """Multipart is recorded as its logical parts, not as raw boundary bytes."""
        metadata = {
            "reason": "The wardrobe door was already loose when I moved in.",
            "expectedStateEpoch": harness.state_epoch,
            "expectedVersion": 1,
        }
        response = harness.client.post(
            "/api/v1/charges/CHG-001/contest",
            files=[
                ("metadata", ("metadata.json", json.dumps(metadata).encode(), "application/json")),
                ("attachments", ("evidence.png", PNG, "image/png")),
            ],
            headers=harness.command_headers(),
        )
        golden(
            "post_charge_contest_200",
            {
                "request": {
                    "contentType": "multipart/form-data",
                    "parts": [
                        {"name": "metadata", "contentType": "application/json", "value": metadata},
                        {
                            "name": "attachments",
                            "filename": "evidence.png",
                            "contentType": "image/png",
                            "sizeBytes": len(PNG),
                        },
                    ],
                },
                "status": response.status_code,
                "response": response.json(),
            },
        )

    def test_pay(self, harness: Harness) -> None:
        request = {"expectedStateEpoch": harness.state_epoch, "expectedVersion": 2}
        response = harness.client.post(
            "/api/v1/charges/CHG-007/pay", json=request, headers=harness.command_headers()
        )
        golden(
            "post_charge_pay_200",
            {"request": request, "status": response.status_code, "response": response.json()},
        )

    def test_create_task(self, harness: Harness) -> None:
        request = {
            "id": "3f7c1e8a-2b4d-4c6e-9a10-5d8b7c2e4f61",
            "bookingId": "BKG-001",
            "category": "furniture",
            "notes": "The wardrobe door hinge has come away from the frame.",
            "location": "Oceanview > Apartment 2 > OVA111",
            "date": "2026-08-01T12:00:00Z",
            "expectedStateEpoch": harness.state_epoch,
        }
        response = harness.client.post("/api/v1/tasks", json=request, headers=harness.command_headers())
        golden(
            "post_task_201",
            {"request": request, "status": response.status_code, "response": response.json()},
        )

    def test_notification_read(self, harness: Harness) -> None:
        request = {"expectedStateEpoch": harness.state_epoch}
        response = harness.client.post("/api/v1/notifications/NTF-001/read", json=request)
        golden(
            "post_notification_read_200",
            {"request": request, "status": response.status_code, "response": response.json()},
        )

    def test_invalid_transition_error(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/charges/CHG-005/accept",
            json={"expectedStateEpoch": harness.state_epoch, "expectedVersion": 3},
            headers=harness.command_headers(),
        )
        golden(
            "error_charge_invalid_transition_409",
            {"status": response.status_code, "response": response.json()},
        )

    def test_version_conflict_error(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/charges/CHG-001/accept",
            json={"expectedStateEpoch": harness.state_epoch, "expectedVersion": 99},
            headers=harness.command_headers(),
        )
        golden(
            "error_charge_version_conflict_409",
            {"status": response.status_code, "response": response.json()},
        )

    def test_epoch_mismatch_error(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/charges/CHG-001/accept",
            json={"expectedStateEpoch": str(uuid.uuid4()), "expectedVersion": 1},
            headers=harness.command_headers(),
        )
        golden(
            "error_store_epoch_mismatch_409",
            {"status": response.status_code, "response": response.json()},
        )

    def test_idempotency_mismatch_error(self, harness: Harness) -> None:
        key = str(uuid.uuid4())
        harness.client.post(
            "/api/v1/charges/CHG-001/accept",
            json={"expectedStateEpoch": harness.state_epoch, "expectedVersion": 1},
            headers={"Idempotency-Key": key},
        )
        response = harness.client.post(
            "/api/v1/charges/CHG-001/accept",
            json={"expectedStateEpoch": harness.state_epoch, "expectedVersion": 2},
            headers={"Idempotency-Key": key},
        )
        golden(
            "error_idempotency_payload_mismatch_409",
            {"status": response.status_code, "response": response.json()},
        )

    def test_not_found_error(self, harness: Harness) -> None:
        response = harness.client.get("/api/v1/charges/CHG-404")
        golden(
            "error_charge_not_found_404",
            {"status": response.status_code, "response": response.json()},
        )

    def test_validation_error(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/tasks",
            json={
                "id": "not-a-uuid",
                "bookingId": "BKG-001",
                "category": "gardening",
                "notes": "short",
                "location": "Oceanview > Apartment 2 > OVA111",
                "date": "2026-08-01T12:00:00Z",
                "expectedStateEpoch": harness.state_epoch,
            },
            headers=harness.command_headers(),
        )
        golden(
            "error_validation_failed_422",
            {"status": response.status_code, "response": response.json()},
        )

    def test_sse_frames(self, harness: Harness) -> None:
        """Logical SSE frames, including the id-less ``sync.required`` signal."""
        harness.client.post(
            "/api/v1/charges/CHG-001/accept",
            json={"expectedStateEpoch": harness.state_epoch, "expectedVersion": 1},
            headers=harness.command_headers(),
        )
        import asyncio

        from app.api.v1.endpoints.events import _render
        from app.services.event_bus import replayable_events

        snapshot = asyncio.run(harness.store.snapshot())
        replay, _ = replayable_events(
            events=snapshot["events"],
            stream_epoch=snapshot["streamEpoch"],
            evicted_through=snapshot["streamEvictedThrough"],
            cursor=f"{snapshot['streamEpoch']}:0:0",
        )
        frames = [_render(event) for event in replay]
        golden(
            "sse_frames",
            {
                "replay": [
                    {"id": "<eventId>", "event": frame["event"], "data": json.loads(frame["data"])}
                    for frame in frames
                ],
                "syncRequired": {
                    "event": "sync.required",
                    "data": {"reason": "cursorUnservable"},
                    "note": "emitted without an id, then the stream closes",
                },
            },
        )
