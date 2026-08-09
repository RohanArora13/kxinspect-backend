"""Accept, Contest, Pay, task create and notification read.

These are the tests that keep the mutation protocol honest: one commit per command,
replay is free, a mismatched digest is a 409, and a deadline that wins the race is durable
even when the command it beat fails.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from app.core.config import MAX_ATTACHMENTS

from tests.conftest import Harness, build_harness, make_settings

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02"
    b"\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01"
    b"\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82"
)
PDF = b"%PDF-1.4\n1 0 obj\n<< >>\nendobj\ntrailer\n<< >>\n%%EOF\n"
REASON = "The wardrobe door was already loose when I moved in and it is in my inventory."


def contest_files(
    *,
    reason: str = REASON,
    epoch: str,
    version: int,
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> list[tuple[str, tuple[str, bytes, str]]]:
    metadata = json.dumps(
        {"reason": reason, "expectedStateEpoch": epoch, "expectedVersion": version}
    ).encode()
    parts: list[tuple[str, tuple[str, bytes, str]]] = [
        ("metadata", ("metadata.json", metadata, "application/json"))
    ]
    for name, body, media_type in attachments or []:
        parts.append(("attachments", (name, body, media_type)))
    return parts


def accept(harness: Harness, charge_id: str, version: int, key: str | None = None) -> httpx.Response:
    return harness.client.post(
        f"/api/v1/charges/{charge_id}/accept",
        json={"expectedStateEpoch": harness.state_epoch, "expectedVersion": version},
        headers=harness.command_headers(key),
    )


class TestAccept:
    def test_accept_moves_outstanding_to_accepted_with_student_origin(self, harness: Harness) -> None:
        response = accept(harness, "CHG-001", 1)
        assert response.status_code == 200, response.text
        charge = response.json()["data"]
        assert charge["status"] == "accepted"
        assert charge["acceptanceOrigin"] == "student"
        assert charge["acceptedAt"] == "2026-08-01T12:00:00Z"
        assert charge["version"] == 2
        assert charge["updatedAt"] == "2026-08-01T12:00:00Z"

    def test_accept_survives_a_restart(self, harness: Harness, settings: Any) -> None:
        accept(harness, "CHG-001", 1)
        harness.client.close()
        harness.store.close()

        _, restarted = build_harness(settings)
        with restarted.client:
            assert restarted.charge("CHG-001")["status"] == "accepted"

    def test_accept_from_a_terminal_state_is_409_with_the_authoritative_charge(
        self, harness: Harness
    ) -> None:
        response = accept(harness, "CHG-005", 3)
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "charge.invalid_transition"
        assert error["details"]["currentCharge"]["id"] == "CHG-005"
        assert error["details"]["status"] == "paid"

    def test_stale_version_is_409_and_returns_the_current_charge(self, harness: Harness) -> None:
        response = accept(harness, "CHG-001", 99)
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "charge.version_conflict"
        assert error["details"]["currentCharge"]["version"] == 1

    def test_wrong_state_epoch_is_409(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/charges/CHG-001/accept",
            json={"expectedStateEpoch": str(uuid.uuid4()), "expectedVersion": 1},
            headers=harness.command_headers(),
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "store.epoch_mismatch"

    def test_unknown_charge_is_404(self, harness: Harness) -> None:
        assert accept(harness, "CHG-404", 1).status_code == 404


class TestIdempotency:
    def test_missing_key_is_400(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/charges/CHG-001/accept",
            json={"expectedStateEpoch": harness.state_epoch, "expectedVersion": 1},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "request.idempotency_key_missing"

    def test_non_uuid_key_is_400(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/charges/CHG-001/accept",
            json={"expectedStateEpoch": harness.state_epoch, "expectedVersion": 1},
            headers={"Idempotency-Key": "not-a-uuid"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "request.idempotency_key_invalid"

    def test_replay_returns_the_committed_response_without_a_second_commit(self, harness: Harness) -> None:
        key = str(uuid.uuid4())
        first = accept(harness, "CHG-001", 1, key)
        revision = harness.store.store_revision
        second = accept(harness, "CHG-001", 1, key)
        assert first.status_code == second.status_code == 200
        assert first.json()["data"] == second.json()["data"]
        assert harness.store.store_revision == revision

    def test_replay_survives_a_restart(self, harness: Harness, settings: Any) -> None:
        key = str(uuid.uuid4())
        first = accept(harness, "CHG-001", 1, key)
        harness.client.close()
        harness.store.close()

        _, restarted = build_harness(settings)
        with restarted.client:
            replay = restarted.client.post(
                "/api/v1/charges/CHG-001/accept",
                json={"expectedStateEpoch": restarted.state_epoch, "expectedVersion": 1},
                headers={"Idempotency-Key": key},
            )
            assert replay.status_code == 200
            assert replay.json()["data"] == first.json()["data"]

    def test_same_key_different_payload_is_409(self, harness: Harness) -> None:
        key = str(uuid.uuid4())
        accept(harness, "CHG-001", 1, key)
        response = harness.client.post(
            "/api/v1/charges/CHG-001/accept",
            json={"expectedStateEpoch": harness.state_epoch, "expectedVersion": 2},
            headers={"Idempotency-Key": key},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "idempotency.payload_mismatch"

    def test_the_same_key_on_another_resource_is_a_different_operation(self, harness: Harness) -> None:
        key = str(uuid.uuid4())
        assert accept(harness, "CHG-001", 1, key).status_code == 200
        # CHG-007 is already accepted, so a genuine second command runs and 409s on
        # transition rather than replaying CHG-001's response.
        other = accept(harness, "CHG-007", 2, key)
        assert other.status_code == 409
        assert other.json()["error"]["code"] == "charge.invalid_transition"

    def test_a_failed_command_is_not_cached(self, harness: Harness) -> None:
        key = str(uuid.uuid4())
        assert accept(harness, "CHG-001", 99, key).status_code == 409
        assert accept(harness, "CHG-001", 1, key).status_code == 200


class TestDeadlineRace:
    def test_reconciliation_commits_even_when_the_racing_command_fails(self, harness: Harness) -> None:
        """CHG-001 expires while the client still believes it is outstanding at v1."""
        harness.clock.advance(timedelta(days=14))
        response = harness.client.post(
            "/api/v1/charges/CHG-001/accept",
            json={"expectedStateEpoch": harness.state_epoch, "expectedVersion": 1},
            headers=harness.command_headers(),
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "charge.version_conflict"

        committed = harness.charge("CHG-001")
        assert committed["status"] == "accepted"
        assert committed["acceptanceOrigin"] == "deadline"
        assert committed["acceptedAt"] == "2026-08-14T09:00:00Z"
        assert committed["version"] == 2

    def test_the_deadline_bumps_the_version_exactly_once(self, harness: Harness) -> None:
        harness.clock.advance(timedelta(days=14))
        harness.client.post(
            "/api/v1/charges/CHG-001/accept",
            json={"expectedStateEpoch": harness.state_epoch, "expectedVersion": 1},
            headers=harness.command_headers(),
        )
        for _ in range(3):
            harness.charge("CHG-001")
        assert harness.charge("CHG-001")["version"] == 2


class TestPay:
    def test_pay_moves_accepted_to_paid(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/charges/CHG-007/pay",
            json={"expectedStateEpoch": harness.state_epoch, "expectedVersion": 2},
            headers=harness.command_headers(),
        )
        assert response.status_code == 200, response.text
        charge = response.json()["data"]
        assert charge["status"] == "paid"
        assert charge["paidAt"] == "2026-08-01T12:00:00Z"
        assert charge["version"] == 3

    def test_pay_on_an_outstanding_charge_is_409(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/charges/CHG-001/pay",
            json={"expectedStateEpoch": harness.state_epoch, "expectedVersion": 1},
            headers=harness.command_headers(),
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "charge.invalid_transition"


class TestContest:
    def test_contest_stores_the_reason_and_finalised_attachments(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/charges/CHG-001/contest",
            files=contest_files(
                epoch=harness.state_epoch,
                version=1,
                attachments=[("evidence.png", PNG, "image/png")],
            ),
            headers=harness.command_headers(),
        )
        assert response.status_code == 200, response.text
        charge = response.json()["data"]
        assert charge["status"] == "contested"
        assert charge["contestReason"] == REASON
        assert charge["contestedAt"] == "2026-08-01T12:00:00Z"
        assert len(charge["contestAttachments"]) == 1

        attachment = charge["contestAttachments"][0]
        assert attachment["displayName"] == "evidence.png"
        assert attachment["mediaType"] == "image/png"
        assert attachment["sizeBytes"] == len(PNG)
        assert attachment["downloadUrl"].startswith("/api/v1/attachments/")
        assert harness.client.get(attachment["downloadUrl"]).content == PNG

    def test_contest_without_attachments_is_accepted(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/charges/CHG-001/contest",
            files=contest_files(epoch=harness.state_epoch, version=1),
            headers=harness.command_headers(),
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["contestAttachments"] == []

    @pytest.mark.parametrize("length", [9, 2001])
    def test_reason_outside_the_grapheme_bounds_is_422(self, harness: Harness, length: int) -> None:
        response = harness.client.post(
            "/api/v1/charges/CHG-001/contest",
            files=contest_files(reason="a" * length, epoch=harness.state_epoch, version=1),
            headers=harness.command_headers(),
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "contest.reason_invalid"

    @pytest.mark.parametrize("length", [10, 2000])
    def test_reason_on_the_grapheme_bounds_is_accepted(self, harness: Harness, length: int) -> None:
        response = harness.client.post(
            "/api/v1/charges/CHG-001/contest",
            files=contest_files(reason="a" * length, epoch=harness.state_epoch, version=1),
            headers=harness.command_headers(),
        )
        assert response.status_code == 200, response.text

    def test_a_2000_grapheme_emoji_reason_is_accepted(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/charges/CHG-001/contest",
            files=contest_files(reason="👍🏽" * 2000, epoch=harness.state_epoch, version=1),
            headers=harness.command_headers(),
        )
        assert response.status_code == 200, response.text

    def test_more_than_five_attachments_is_rejected(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/charges/CHG-001/contest",
            files=contest_files(
                epoch=harness.state_epoch,
                version=1,
                attachments=[(f"e{i}.png", PNG, "image/png") for i in range(MAX_ATTACHMENTS + 1)],
            ),
            headers=harness.command_headers(),
        )
        assert response.status_code in {400, 422}
        assert response.json()["error"]["code"] in {"attachment.too_many", "request.malformed"}

    def test_exactly_five_attachments_is_accepted(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/charges/CHG-001/contest",
            files=contest_files(
                epoch=harness.state_epoch,
                version=1,
                attachments=[(f"e{i}.png", PNG, "image/png") for i in range(MAX_ATTACHMENTS)],
            ),
            headers=harness.command_headers(),
        )
        assert response.status_code == 200, response.text
        assert len(response.json()["data"]["contestAttachments"]) == MAX_ATTACHMENTS

    def test_an_unsupported_media_type_is_415(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/charges/CHG-001/contest",
            files=contest_files(
                epoch=harness.state_epoch,
                version=1,
                attachments=[("notes.txt", b"plain text", "text/plain")],
            ),
            headers=harness.command_headers(),
        )
        assert response.status_code == 415
        assert response.json()["error"]["code"] == "attachment.unsupported_media_type"

    def test_a_lying_extension_is_rejected(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/charges/CHG-001/contest",
            files=contest_files(
                epoch=harness.state_epoch,
                version=1,
                attachments=[("evidence.png", PDF, "image/png")],
            ),
            headers=harness.command_headers(),
        )
        assert response.status_code == 415

    def test_an_empty_attachment_is_422(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/charges/CHG-001/contest",
            files=contest_files(
                epoch=harness.state_epoch,
                version=1,
                attachments=[("empty.png", b"", "image/png")],
            ),
            headers=harness.command_headers(),
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "attachment.empty"

    def test_a_traversal_filename_is_sanitised_not_honoured(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/charges/CHG-001/contest",
            files=contest_files(
                epoch=harness.state_epoch,
                version=1,
                attachments=[("../../etc/passwd.png", PNG, "image/png")],
            ),
            headers=harness.command_headers(),
        )
        assert response.status_code == 200, response.text
        attachment = response.json()["data"]["contestAttachments"][0]
        assert attachment["displayName"] == "passwd.png"
        assert "/" not in attachment["id"]

    def test_a_failed_contest_leaves_no_attachment_behind(self, harness: Harness) -> None:
        uploads = harness.settings.uploads_root
        before = sorted(p.name for p in uploads.iterdir()) if uploads.exists() else []
        response = harness.client.post(
            "/api/v1/charges/CHG-005/contest",
            files=contest_files(
                epoch=harness.state_epoch,
                version=3,
                attachments=[("evidence.png", PNG, "image/png")],
            ),
            headers=harness.command_headers(),
        )
        assert response.status_code == 409
        after = sorted(p.name for p in uploads.iterdir()) if uploads.exists() else []
        assert after == before
        assert not list(harness.settings.staging_root.iterdir())

    def test_replaying_a_contest_creates_no_second_attachment(self, harness: Harness) -> None:
        key = str(uuid.uuid4())
        files = contest_files(
            epoch=harness.state_epoch, version=1, attachments=[("evidence.png", PNG, "image/png")]
        )
        first = harness.client.post(
            "/api/v1/charges/CHG-001/contest", files=files, headers={"Idempotency-Key": key}
        )
        assert first.status_code == 200, first.text
        uploaded = sorted(p.name for p in harness.settings.uploads_root.iterdir())

        replay = harness.client.post(
            "/api/v1/charges/CHG-001/contest",
            files=contest_files(
                epoch=harness.state_epoch,
                version=1,
                attachments=[("evidence.png", PNG, "image/png")],
            ),
            headers={"Idempotency-Key": key},
        )
        assert replay.status_code == 200
        assert replay.json()["data"] == first.json()["data"]
        assert sorted(p.name for p in harness.settings.uploads_root.iterdir()) == uploaded

    def test_a_different_attachment_set_under_the_same_key_is_409(self, harness: Harness) -> None:
        key = str(uuid.uuid4())
        harness.client.post(
            "/api/v1/charges/CHG-001/contest",
            files=contest_files(
                epoch=harness.state_epoch,
                version=1,
                attachments=[("evidence.png", PNG, "image/png")],
            ),
            headers={"Idempotency-Key": key},
        )
        response = harness.client.post(
            "/api/v1/charges/CHG-001/contest",
            files=contest_files(
                epoch=harness.state_epoch,
                version=1,
                attachments=[("other.pdf", PDF, "application/pdf")],
            ),
            headers={"Idempotency-Key": key},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "idempotency.payload_mismatch"

    def test_a_non_multipart_body_is_400(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/charges/CHG-001/contest",
            json={"reason": REASON},
            headers=harness.command_headers(),
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "request.malformed"

    def test_a_missing_metadata_part_is_400(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/charges/CHG-001/contest",
            files=[("attachments", ("evidence.png", PNG, "image/png"))],
            headers=harness.command_headers(),
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "request.malformed"

    def test_metadata_declaring_the_wrong_content_type_is_400(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/charges/CHG-001/contest",
            files=[("metadata", ("metadata.txt", b"{}", "text/plain"))],
            headers=harness.command_headers(),
        )
        assert response.status_code == 400


class TestCreateTask:
    def _payload(self, harness: Harness, **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "bookingId": "BKG-001",
            "category": "furniture",
            "notes": "The wardrobe door hinge has come away from the frame.",
            "location": "Oceanview > Apartment 2 > OVA111",
            "date": "2026-08-01T12:00:00Z",
            "expectedStateEpoch": harness.state_epoch,
        }
        payload.update(overrides)
        return payload

    def test_task_is_created_with_server_owned_status(self, harness: Harness) -> None:
        payload = self._payload(harness)
        response = harness.client.post("/api/v1/tasks", json=payload, headers=harness.command_headers())
        assert response.status_code == 201, response.text
        task = response.json()["data"]
        assert task["id"] == payload["id"]
        assert task["status"] == "new"

        hub = harness.client.get("/api/v1/bookings/BKG-001/hub").json()["data"]
        assert payload["id"] in {row["id"] for row in hub["tasks"]}

    def test_a_non_uuid_task_id_is_422(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/tasks",
            json=self._payload(harness, id="TCK99"),
            headers=harness.command_headers(),
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation.failed"

    def test_an_unknown_category_is_422(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/tasks",
            json=self._payload(harness, category="gardening"),
            headers=harness.command_headers(),
        )
        assert response.status_code == 422

    def test_notes_shorter_than_ten_graphemes_are_rejected(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/tasks",
            json=self._payload(harness, notes="broken"),
            headers=harness.command_headers(),
        )
        assert response.status_code == 422

    def test_an_unknown_booking_is_404(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/tasks",
            json=self._payload(harness, bookingId="BKG-404"),
            headers=harness.command_headers(),
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "booking.not_found"

    def test_a_duplicate_task_id_under_a_new_key_is_409(self, harness: Harness) -> None:
        payload = self._payload(harness)
        assert (
            harness.client.post("/api/v1/tasks", json=payload, headers=harness.command_headers()).status_code
            == 201
        )
        response = harness.client.post("/api/v1/tasks", json=payload, headers=harness.command_headers())
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "task.duplicate_id"

    def test_replay_returns_the_same_201(self, harness: Harness) -> None:
        payload = self._payload(harness)
        key = str(uuid.uuid4())
        first = harness.client.post("/api/v1/tasks", json=payload, headers={"Idempotency-Key": key})
        second = harness.client.post("/api/v1/tasks", json=payload, headers={"Idempotency-Key": key})
        assert first.status_code == second.status_code == 201
        assert first.json()["data"] == second.json()["data"]

    def test_extra_fields_are_rejected(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/tasks",
            json=self._payload(harness, status="completed"),
            headers=harness.command_headers(),
        )
        assert response.status_code == 422


class TestNotificationRead:
    def test_marking_read_requires_no_idempotency_key(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/notifications/NTF-001/read",
            json={"expectedStateEpoch": harness.state_epoch},
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["read"] is True

    def test_marking_read_twice_is_a_no_op(self, harness: Harness) -> None:
        harness.client.post(
            "/api/v1/notifications/NTF-001/read",
            json={"expectedStateEpoch": harness.state_epoch},
        )
        revision = harness.store.store_revision
        response = harness.client.post(
            "/api/v1/notifications/NTF-001/read",
            json={"expectedStateEpoch": harness.state_epoch},
        )
        assert response.status_code == 200
        assert harness.store.store_revision == revision

    def test_epoch_is_still_checked(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/notifications/NTF-001/read",
            json={"expectedStateEpoch": str(uuid.uuid4())},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "store.epoch_mismatch"

    def test_unknown_notification_is_404(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/notifications/NTF-404/read",
            json={"expectedStateEpoch": harness.state_epoch},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "notification.not_found"


class TestMalformedBodies:
    def test_invalid_json_is_400(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/charges/CHG-001/accept",
            content=b"{not json",
            headers={**harness.command_headers(), "Content-Type": "application/json"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "request.malformed"

    def test_a_float_amount_style_body_is_rejected_by_strict_validation(self, harness: Harness) -> None:
        response = harness.client.post(
            "/api/v1/charges/CHG-001/accept",
            json={"expectedStateEpoch": harness.state_epoch, "expectedVersion": 1.0},
            headers=harness.command_headers(),
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation.failed"

    def test_validation_details_never_echo_user_text(self, harness: Harness) -> None:
        secret = "please do not leak this reason text"
        response = harness.client.post(
            "/api/v1/charges/CHG-001/contest",
            files=[
                (
                    "metadata",
                    (
                        "metadata.json",
                        json.dumps({"reason": secret, "expectedVersion": "one"}).encode(),
                        "application/json",
                    ),
                )
            ],
            headers=harness.command_headers(),
        )
        assert response.status_code == 422
        assert secret not in response.text


def test_settings_reject_multi_worker(tmp_path: Any) -> None:
    with pytest.raises(ValueError, match="one Uvicorn worker"):
        make_settings(tmp_path, workers=2)


def test_dev_routes_require_a_token_value(tmp_path: Any) -> None:
    with pytest.raises(ValueError, match="KX_DEV_TOKEN"):
        make_settings(tmp_path, enable_dev_routes=True, dev_token="")
