#!/usr/bin/env python
"""Validate a fixture bundle against its own manifest.

Usage::

    uv run python scripts/verify_fixture_manifest.py [--bundle <dir>]

With no ``--bundle`` the exporter is run into a temporary directory and the result is
checked, which is what backend CI needs. Pointing ``--bundle`` at the frontend's committed
``assets/fixtures`` checks that copy instead, with no network and no sibling assumption.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - script bootstrap
    sys.path.insert(0, str(REPO_ROOT))

from app.core.canonical_json import canonical_json  # noqa: E402
from app.core.config import CONTRACT_VERSION, REFERENCE_NOW, SCHEMA_VERSION, SEED_VERSION  # noqa: E402

from scripts.export_fixtures import export  # noqa: E402


def verify(bundle: Path) -> list[str]:
    """Return a list of problems; empty means the bundle is self-consistent."""
    problems: list[str] = []
    manifest_path = bundle / "manifest.json"
    if not manifest_path.is_file():
        return [f"missing manifest at {manifest_path}"]
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))

    for key, expected in (
        ("schemaVersion", SCHEMA_VERSION),
        ("contractVersion", CONTRACT_VERSION),
        ("seedVersion", SEED_VERSION),
        ("referenceNow", REFERENCE_NOW),
    ):
        if manifest.get(key) != expected:
            problems.append(f"manifest {key} is {manifest.get(key)!r}, expected {expected!r}")

    listed = {entry["path"] for entry in manifest["files"]}
    if "manifest.json" in listed:
        problems.append("manifest must exclude itself from its own file list")

    on_disk = {
        path.relative_to(bundle).as_posix()
        for path in bundle.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    for missing in sorted(listed - on_disk):
        problems.append(f"manifest lists a file that is absent: {missing}")
    for extra in sorted(on_disk - listed):
        problems.append(f"bundle contains an unlisted file: {extra}")

    paths = [entry["path"] for entry in manifest["files"]]
    if paths != sorted(paths):
        problems.append("manifest file list is not in bytewise sorted order")

    for entry in manifest["files"]:
        path = bundle / entry["path"]
        if not path.is_file():
            continue
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            problems.append(f"sha256 mismatch for {entry['path']}")
        if len(data) != entry["sizeBytes"]:
            problems.append(f"sizeBytes mismatch for {entry['path']}")

    recomputed = hashlib.sha256(
        canonical_json([[entry["path"], entry["sha256"]] for entry in manifest["files"]])
    ).hexdigest()
    if recomputed != manifest.get("bundleDigest"):
        problems.append("bundleDigest does not match the listed file hashes")

    state = json.loads((bundle / manifest["dataFile"]).read_text(encoding="utf-8"))
    for key in ("bookings", "inventoryReports", "inspections", "tasks", "charges", "notifications"):
        if key not in state:
            problems.append(f"{manifest['dataFile']} is missing '{key}'")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bundle",
        type=Path,
        default=None,
        help="bundle directory to verify; regenerates into a temp directory when omitted",
    )
    args = parser.parse_args(argv)

    if args.bundle is None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "bundle"
            manifest = export(target)
            problems = verify(target)
            digest = manifest["bundleDigest"]
    else:
        target = args.bundle.resolve()
        problems = verify(target)
        digest = json.loads((target / "manifest.json").read_text(encoding="utf-8")).get(
            "bundleDigest", "<absent>"
        )

    if problems:
        print(f"fixture manifest verification failed for {target}:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"fixture manifest verified: {target}")
    print(f"bundleDigest {digest}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
