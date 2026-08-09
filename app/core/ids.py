"""Injected identifier generation so tests get deterministic request and entity ids."""

from __future__ import annotations

import uuid
from typing import Protocol


class IdGenerator(Protocol):
    def next_uuid(self) -> str: ...


class UuidGenerator:
    def next_uuid(self) -> str:
        return str(uuid.uuid4())


class SequenceIdGenerator:
    """Deterministic generator: ``<prefix>-0001``, ``<prefix>-0002``, ..."""

    def __init__(self, prefix: str = "00000000-0000-4000-8000") -> None:
        self._prefix = prefix
        self._counter = 0

    def next_uuid(self) -> str:
        self._counter += 1
        return f"{self._prefix}-{self._counter:012d}"
