#!/usr/bin/env python
"""Deliberate destructive reset of a runtime directory.

Usage::

    uv run python scripts/reset_runtime.py --runtime-root <explicit-path>

This script deletes data, so it refuses far more than it accepts. The target must be an
explicit, absolute, non-symlink directory that contains this application's marker file
with the expected UUID, and it must not be ``/``, a home directory, or the repository root
or any ancestor of it. Only the named children are removed — never a recursive wipe of an
arbitrary root.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - script bootstrap
    sys.path.insert(0, str(REPO_ROOT))

from app.core.canonical_json import canonical_json  # noqa: E402
from app.core.config import RUNTIME_MARKER_UUID  # noqa: E402
from app.db.store import _RuntimeFileLock  # noqa: E402

#: Only these children are ever removed.
REMOVABLE_FILES = ("state.json", ".kxinspect-runtime")
REMOVABLE_DIRECTORIES = ("uploads",)
REMOVABLE_GLOBS = ("state.json.*.tmp",)


class UnsafeTargetError(Exception):
    """The supplied path failed a safety check; nothing was deleted."""


def assert_safe(target: Path) -> Path:
    """Refuse anything that is not unmistakably an app-owned runtime directory."""
    if not target.is_absolute():
        raise UnsafeTargetError(f"runtime root must be absolute: {target}")
    if target.is_symlink():
        raise UnsafeTargetError(f"runtime root must not be a symlink: {target}")
    resolved = target.resolve()
    if resolved != target:
        raise UnsafeTargetError(f"runtime root must already be fully resolved: {target}")
    if not resolved.is_dir():
        raise UnsafeTargetError(f"runtime root is not a directory: {resolved}")
    if resolved == Path(resolved.anchor):
        raise UnsafeTargetError("refusing to reset a filesystem root")
    if resolved == Path.home() or resolved in Path.home().parents:
        raise UnsafeTargetError("refusing to reset a home directory or an ancestor of it")
    if resolved == REPO_ROOT or resolved in REPO_ROOT.parents:
        raise UnsafeTargetError("refusing to reset the repository root or an ancestor of it")

    marker = resolved / ".kxinspect-runtime"
    if not marker.is_file():
        raise UnsafeTargetError(f"missing app-owned marker file: {marker}")
    try:
        found = uuid.UUID(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as exc:
        raise UnsafeTargetError(f"marker file is unreadable or malformed: {marker}") from exc
    if str(found) != RUNTIME_MARKER_UUID:
        raise UnsafeTargetError(f"marker uuid does not match this application: {marker}")
    return resolved


def reset(target: Path) -> list[str]:
    """Remove only the named runtime children, under the exclusive runtime lock."""
    safe = assert_safe(target)
    lock = _RuntimeFileLock(safe / "runtime.lock")
    lock.acquire()
    removed: list[str] = []
    try:
        for name in REMOVABLE_FILES:
            path = safe / name
            if path.is_file():
                path.unlink()
                removed.append(name)
        for pattern in REMOVABLE_GLOBS:
            for path in sorted(safe.glob(pattern)):
                path.unlink()
                removed.append(path.name)
        for name in REMOVABLE_DIRECTORIES:
            path = safe / name
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
                removed.append(f"{name}/")
    finally:
        lock.release()
    (safe / "runtime.lock").unlink(missing_ok=True)
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument(
        "--evidence",
        type=Path,
        default=None,
        help="optional path for a deterministic JSON record of what was removed",
    )
    args = parser.parse_args(argv)

    try:
        removed = reset(args.runtime_root)
    except UnsafeTargetError as exc:
        print(f"refusing to reset: {exc}", file=sys.stderr)
        return 2

    print(f"runtime reset: {args.runtime_root}")
    for name in removed:
        print(f"  removed {name}")
    print("next start reloads the seed and generates a new stateEpoch")
    if args.evidence is not None:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_bytes(
            canonical_json({"runtimeRoot": str(args.runtime_root), "removed": removed}) + b"\n"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
