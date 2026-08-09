#!/usr/bin/env python
"""Build the deterministic fixture bundle consumed by the Flutter app.

Usage::

    uv run python scripts/export_fixtures.py --output <explicit-dir>

The output directory is always explicit: the exporter never assumes a sibling checkout,
so backend CI can regenerate the bundle into a temporary directory and byte-compare it
with whatever the frontend has committed.

Bundle layout (every file except ``manifest.json`` is hashed into the manifest)::

    manifest.json
    app_state.json
    vectors/charge_state_vectors.json
    vectors/deadline_vectors.json
    vectors/namespace_vectors.json
    media/<opaqueId>.<ext>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - script bootstrap
    sys.path.insert(0, str(REPO_ROOT))

from app.core.canonical_json import canonical_json  # noqa: E402
from app.core.clock import format_instant, parse_instant  # noqa: E402
from app.core.config import (  # noqa: E402
    API_MAJOR,
    CONTRACT_VERSION,
    REFERENCE_NOW,
    SCHEMA_VERSION,
    SEED_VERSION,
)
from app.db.models import ENTITY_KEYS, SEED_FILES  # noqa: E402
from app.domain.charge_state import state_vectors  # noqa: E402
from app.domain.deadline import deadline_for, is_expired  # noqa: E402
from app.domain.namespace import database_name, fixture_namespace, remote_namespace  # noqa: E402

LOCALE = "en-GB"
TIME_ZONE = "Europe/London"

SEED_DIR = REPO_ROOT / "app" / "seed"
MEDIA_DIR = REPO_ROOT / "app" / "static" / "photos"


class ValidationFailureError(Exception):
    """A seed invariant is broken; the bundle is not written."""


def _load(name: str) -> list[dict[str, Any]]:
    payload = json.loads((SEED_DIR / name).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValidationFailureError(f"{name} must contain a JSON array")
    return payload


def load_seed() -> dict[str, list[dict[str, Any]]]:
    return {key: _load(SEED_FILES[key]) for key in ENTITY_KEYS}


def _opaque(url: str) -> str:
    return url.rsplit("/", 1)[-1]


def collect_media_references(seed: dict[str, list[dict[str, Any]]]) -> set[str]:
    referenced: set[str] = set()
    for charge in seed["charges"]:
        for photo in charge["photos"]:
            referenced.add(_opaque(photo["url"]))
            referenced.add(_opaque(photo["thumbnailUrl"]))
        for attachment in charge["contestAttachments"]:
            referenced.add(_opaque(attachment["downloadUrl"]))
            if attachment.get("thumbnailUrl"):
                referenced.add(_opaque(attachment["thumbnailUrl"]))
    for report in seed["inventoryReports"]:
        referenced.add(_opaque(report["reportUrl"]))
    for inspection in seed["inspections"]:
        for action in inspection["itemActions"]:
            if action.get("thumbnailUrl"):
                referenced.add(_opaque(action["thumbnailUrl"]))
    return referenced


def validate(seed: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Prove the invariants the frontend is allowed to assume, then report coverage."""
    problems: list[str] = []

    for key, rows in seed.items():
        ids = [row["id"] for row in rows]
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        if duplicates:
            problems.append(f"{key}: duplicate ids {duplicates}")

    booking_ids = {row["id"] for row in seed["bookings"]}
    inspection_ids = {row["id"] for row in seed["inspections"]}
    charge_ids = {row["id"] for row in seed["charges"]}

    for key in ("inventoryReports", "inspections", "tasks", "charges"):
        for row in seed[key]:
            if row["bookingId"] not in booking_ids:
                problems.append(f"{key}/{row['id']}: unknown bookingId {row['bookingId']}")

    for charge in seed["charges"]:
        if charge["inspectionId"] is not None and charge["inspectionId"] not in inspection_ids:
            problems.append(f"charges/{charge['id']}: unknown inspectionId")
        raised = parse_instant(charge["raisedAt"])
        expected_deadline = deadline_for(raised, charge["gracePeriodDays"])
        if format_instant(expected_deadline) != charge["deadlineAt"]:
            problems.append(f"charges/{charge['id']}: deadlineAt is not raisedAt + gracePeriodDays")
        problems.extend(_status_invariants(charge))

    for inspection in seed["inspections"]:
        for action in inspection["itemActions"]:
            if action["chargeId"] is not None and action["chargeId"] not in charge_ids:
                problems.append(f"inspections/{inspection['id']}: unknown chargeId on item action")
            has_charge = action["chargeId"] is not None
            has_amount = action["amountMinor"] is not None
            if has_charge != has_amount:
                problems.append(
                    f"inspections/{inspection['id']}/{action['id']}: chargeId and amountMinor "
                    "must both be present or both absent"
                )

    for notification in seed["notifications"]:
        if notification["chargeId"] is not None and notification["chargeId"] not in charge_ids:
            problems.append(f"notifications/{notification['id']}: unknown chargeId")

    reference_now = parse_instant(REFERENCE_NOW)
    statuses = {charge["status"] for charge in seed["charges"]}
    missing_statuses = sorted({"outstanding", "accepted", "contested", "resolved", "paid"} - statuses)
    if missing_statuses:
        problems.append(f"charges do not cover every status: missing {missing_statuses}")

    origins = {c["acceptanceOrigin"] for c in seed["charges"] if c["acceptanceOrigin"]}
    missing_origins = sorted({"student", "deadline", "operator"} - origins)
    if missing_origins:
        problems.append(f"charges do not cover every acceptance origin: missing {missing_origins}")

    boundary = [c["id"] for c in seed["charges"] if c["deadlineAt"] == REFERENCE_NOW]
    if not boundary:
        problems.append("no charge has deadlineAt exactly equal to referenceNow")

    busy_bookings = {c["bookingId"] for c in seed["charges"]}
    empty_bookings = sorted(booking_ids - busy_bookings - {r["bookingId"] for r in seed["tasks"]})
    if not empty_bookings:
        problems.append("no booking produces an empty Hub")
    if len(booking_ids) < 2:
        problems.append("at least two bookings are required")

    currencies = {charge["currency"] for charge in seed["charges"]}
    if len(currencies) < 2:
        problems.append("at least two currencies are required to exercise mixed-currency totals")

    missing_media = sorted(
        opaque for opaque in collect_media_references(seed) if not list(MEDIA_DIR.glob(f"{opaque}.*"))
    )
    if missing_media:
        problems.append(f"referenced media files are missing: {missing_media}")

    for attachment in (a for c in seed["charges"] for a in c["contestAttachments"]):
        candidates = list(MEDIA_DIR.glob(f"{_opaque(attachment['downloadUrl'])}.*"))
        actual = hashlib.sha256(candidates[0].read_bytes()).hexdigest()
        if actual != attachment["sha256"]:
            problems.append(f"contest attachment {attachment['id']}: sha256 does not match bytes")
        if candidates[0].stat().st_size != attachment["sizeBytes"]:
            problems.append(f"contest attachment {attachment['id']}: sizeBytes does not match bytes")

    if problems:
        raise ValidationFailureError("\n".join(f"  - {item}" for item in problems))

    expired = [c["id"] for c in seed["charges"] if is_expired(reference_now, parse_instant(c["deadlineAt"]))]
    return {
        "statuses": sorted(statuses),
        "acceptanceOrigins": sorted(origins),
        "deadlineBoundaryCharges": sorted(boundary),
        "emptyHubBookings": empty_bookings,
        "currencies": sorted(currencies),
        "expiredAtReferenceNow": sorted(expired),
    }


def _status_invariants(charge: dict[str, Any]) -> list[str]:
    """Timestamp/origin rules the client relies on when rendering banners and History."""
    problems: list[str] = []
    status = charge["status"]
    cid = charge["id"]
    if status == "accepted" and (not charge["acceptedAt"] or not charge["acceptanceOrigin"]):
        problems.append(f"charges/{cid}: accepted requires acceptedAt and acceptanceOrigin")
    if status == "outstanding" and any(
        charge[field] for field in ("acceptedAt", "contestedAt", "resolvedAt", "paidAt")
    ):
        problems.append(f"charges/{cid}: outstanding must have no lifecycle timestamps")
    if status == "contested" and not charge["contestedAt"]:
        problems.append(f"charges/{cid}: contested requires contestedAt")
    if status == "resolved" and not charge["resolvedAt"]:
        problems.append(f"charges/{cid}: resolved requires resolvedAt")
    if status == "paid" and (not charge["paidAt"] or not charge["acceptedAt"]):
        problems.append(f"charges/{cid}: paid requires acceptedAt and paidAt")
    if charge["contestReason"] is None and charge["contestAttachments"]:
        problems.append(f"charges/{cid}: attachments without a contest reason")
    if charge["version"] < 1:
        problems.append(f"charges/{cid}: version must start at 1")
    return problems


def deadline_vectors() -> list[dict[str, Any]]:
    """Boundary cases both runtimes must agree on."""
    base = parse_instant("2026-03-01T09:00:00Z")
    cases: list[dict[str, Any]] = []
    for grace in (0, 1, 30, 31, 365):
        deadline = deadline_for(base, grace)
        cases.append(
            {
                "raisedAt": format_instant(base),
                "gracePeriodDays": grace,
                "deadlineAt": format_instant(deadline),
            }
        )
    # Leap day, month rollover and the exact-boundary comparison.
    leap = parse_instant("2028-01-30T23:30:00Z")
    cases.append(
        {
            "raisedAt": format_instant(leap),
            "gracePeriodDays": 30,
            "deadlineAt": format_instant(deadline_for(leap, 30)),
        }
    )
    dst = parse_instant("2026-03-29T00:30:00Z")  # Europe/London clocks change this day.
    cases.append(
        {
            "raisedAt": format_instant(dst),
            "gracePeriodDays": 1,
            "deadlineAt": format_instant(deadline_for(dst, 1)),
        }
    )

    boundary = parse_instant("2026-08-01T12:00:00Z")
    expiry = [
        {
            "now": format_instant(boundary + timedelta(seconds=offset)),
            "deadlineAt": format_instant(boundary),
            "expired": is_expired(boundary + timedelta(seconds=offset), boundary),
        }
        for offset in (-1, 0, 1)
    ]
    return [{"deadlineFor": cases, "isExpired": expiry}]


def namespace_vectors() -> list[dict[str, Any]]:
    """Normalisation and hashing cases named by gate G-01."""
    fixture = fixture_namespace(CONTRACT_VERSION)
    cases: list[dict[str, Any]] = [
        {
            "kind": "fixture",
            "input": None,
            "namespace": fixture,
            "databaseName": database_name(fixture),
        }
    ]
    remote_inputs = [
        "http://127.0.0.1:8000",
        "http://127.0.0.1:8000/",
        "HTTP://127.0.0.1:8000",
        "http://localhost:80",
        "http://localhost",
        "https://api.example.com",
        "https://API.Example.com:443/",
        "https://api.example.com/kx/v1",
        "https://api.example.com/kx/v1/",
        "http://[::1]:8000",
        "https://xn--bcher-kva.example",
        "https://bücher.example",
    ]
    for raw in remote_inputs:
        namespace = remote_namespace(base_url=raw, api_major=API_MAJOR, schema_major=SCHEMA_VERSION)
        cases.append(
            {
                "kind": "remote",
                "input": raw,
                "namespace": namespace,
                "databaseName": database_name(namespace),
            }
        )
    rejected = [
        "",
        "ftp://example.com",
        "https://example.com?x=1",
        "https://example.com#frag",
        "https://user:pass@example.com",
        "https://",
    ]
    return [{"accepted": cases, "rejected": rejected}]


def build_manifest(output: Path, coverage: dict[str, Any], media: list[str]) -> dict[str, Any]:
    """Hash every emitted file except the manifest itself, in POSIX bytewise order."""
    files: list[dict[str, Any]] = []
    for path in sorted(output.rglob("*"), key=lambda item: item.relative_to(output).as_posix()):
        if not path.is_file() or path.name == "manifest.json":
            continue
        data = path.read_bytes()
        files.append(
            {
                "path": path.relative_to(output).as_posix(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "sizeBytes": len(data),
            }
        )
    bundle_digest = hashlib.sha256(
        canonical_json([[item["path"], item["sha256"]] for item in files])
    ).hexdigest()
    return {
        "schemaVersion": SCHEMA_VERSION,
        "contractVersion": CONTRACT_VERSION,
        "seedVersion": SEED_VERSION,
        "referenceNow": REFERENCE_NOW,
        "locale": LOCALE,
        "timeZone": TIME_ZONE,
        "dataFile": "app_state.json",
        "mediaDirectory": "media",
        "mediaIds": media,
        "coverage": coverage,
        "files": files,
        "bundleDigest": bundle_digest,
    }


def write_json(path: Path, payload: object) -> None:
    """Canonical bytes plus a trailing newline; identical on every platform."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(payload) + b"\n")


def export(output: Path) -> dict[str, Any]:
    seed = load_seed()
    coverage = validate(seed)

    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    write_json(
        output / "app_state.json",
        {
            "schemaVersion": SCHEMA_VERSION,
            "contractVersion": CONTRACT_VERSION,
            "seedVersion": SEED_VERSION,
            "referenceNow": REFERENCE_NOW,
            **seed,
        },
    )
    write_json(output / "vectors" / "charge_state_vectors.json", state_vectors())
    write_json(output / "vectors" / "deadline_vectors.json", deadline_vectors())
    write_json(output / "vectors" / "namespace_vectors.json", namespace_vectors())

    media_ids: list[str] = []
    referenced = collect_media_references(seed)
    for opaque in sorted(referenced):
        source = next(iter(sorted(MEDIA_DIR.glob(f"{opaque}.*"))))
        destination = output / "media" / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
        media_ids.append(source.name)

    manifest = build_manifest(output, coverage, media_ids)
    write_json(output / "manifest.json", manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="explicit output directory")
    args = parser.parse_args(argv)
    try:
        manifest = export(args.output.resolve())
    except ValidationFailureError as exc:
        print("fixture validation failed:", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 1
    print(f"bundle written to {args.output}")
    print(f"bundleDigest {manifest['bundleDigest']}")
    print(f"files {len(manifest['files'])}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
