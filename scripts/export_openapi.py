#!/usr/bin/env python
"""Emit or verify the normalised OpenAPI snapshot.

Usage::

    uv run python scripts/export_openapi.py --output docs/openapi-v1.json
    uv run python scripts/export_openapi.py --check docs/openapi-v1.json

``--check`` is what CI runs: it regenerates the document in memory and byte-compares it
with the frozen snapshot. WP-01B may never refreeze the snapshot itself — a mismatch is a
contract change request, not a file to overwrite.

Dev routes are always included so the snapshot does not depend on local environment.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - script bootstrap
    sys.path.insert(0, str(REPO_ROOT))

from app.core.clock import ManualClock, ManualScheduler, parse_instant  # noqa: E402
from app.core.config import REFERENCE_NOW, Settings  # noqa: E402
from app.core.ids import SequenceIdGenerator  # noqa: E402
from app.db.store import JsonStore  # noqa: E402
from app.main import build_app  # noqa: E402


def generate() -> dict[str, Any]:
    """Build the app with a fixed clock and ids so the document is deterministic."""
    settings = Settings(
        runtime_root=Path("runtime"),
        enable_dev_routes=True,
        dev_token="openapi-export",
    )
    clock = ManualClock(parse_instant(REFERENCE_NOW))
    store = JsonStore(settings, clock)
    app = build_app(settings, store, clock, ManualScheduler(), SequenceIdGenerator())
    document: dict[str, Any] = app.openapi()
    return document


def normalize(document: dict[str, Any]) -> bytes:
    """Sorted, minimally separated JSON with a trailing newline.

    RFC 8785 is not used here: JSON Schema keywords such as ``exclusiveMinimum`` are
    floats, which the canonical encoder deliberately refuses. Sorted keys plus fixed
    separators are enough to make the snapshot byte-comparable.
    """
    return (
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        + b"\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--output", type=Path, help="write the snapshot to this path")
    group.add_argument("--check", type=Path, help="byte-compare against this snapshot")
    args = parser.parse_args(argv)

    payload = normalize(generate())

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(payload)
        print(f"openapi written to {args.output} ({len(payload)} bytes)")
        return 0

    snapshot: Path = args.check
    if not snapshot.is_file():
        print(f"frozen snapshot not found: {snapshot}", file=sys.stderr)
        return 1
    committed = snapshot.read_bytes()
    if committed == payload:
        print(f"openapi matches the frozen snapshot: {snapshot}")
        return 0

    print(f"openapi drift detected against {snapshot}", file=sys.stderr)
    generated_paths = set(json.loads(payload)["paths"])
    committed_paths = set(json.loads(committed)["paths"])
    for added in sorted(generated_paths - committed_paths):
        print(f"  + path {added}", file=sys.stderr)
    for removed in sorted(committed_paths - generated_paths):
        print(f"  - path {removed}", file=sys.stderr)
    if generated_paths == committed_paths:
        print("  paths match; a schema or description changed", file=sys.stderr)
    print(
        "  file a G-01 contract change request; do not overwrite the snapshot from WP-01B",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
