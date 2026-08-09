"""RFC 8785 (JSON Canonicalization Scheme) serialisation.

The contract uses canonical JSON for two things that must be byte-identical across
processes, languages and restarts: the fixture bundle manifest hashes and the
idempotency request digest. Both are compared bytewise, so this module refuses any
value whose canonical form would be ambiguous (non-finite or fractional numbers).
"""

from __future__ import annotations

import hashlib
from typing import Final

JsonValue = None | bool | int | str | list["JsonValue"] | dict[str, "JsonValue"]

_ESCAPES: Final[dict[int, str]] = {
    0x08: "\\b",
    0x09: "\\t",
    0x0A: "\\n",
    0x0C: "\\f",
    0x0D: "\\r",
    0x22: '\\"',
    0x5C: "\\\\",
}

# ECMAScript Number.MAX_SAFE_INTEGER: outside this range integer round-trips are lossy.
_MAX_SAFE_INTEGER: Final[int] = 2**53 - 1


class CanonicalJsonError(ValueError):
    """Raised when a value cannot be canonicalised without ambiguity."""


def _escape(value: str) -> str:
    out: list[str] = ['"']
    for char in value:
        code = ord(char)
        escape = _ESCAPES.get(code)
        if escape is not None:
            out.append(escape)
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _utf16_sort_key(key: str) -> bytes:
    """RFC 8785 orders members by UTF-16 code unit, not by Unicode code point."""
    return key.encode("utf-16-be", errors="surrogatepass")


def _serialize(value: object) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        if not -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER:
            raise CanonicalJsonError(f"integer outside the safe range: {value}")
        return str(value)
    if isinstance(value, float):
        raise CanonicalJsonError(
            "floating point values are not part of the contract; money uses integer minor units"
        )
    if isinstance(value, str):
        return _escape(value)
    if isinstance(value, list):
        return "[" + ",".join(_serialize(item) for item in value) + "]"
    if isinstance(value, dict):
        members = sorted(value.items(), key=lambda item: _utf16_sort_key(item[0]))
        for key, _ in members:
            if not isinstance(key, str):
                raise CanonicalJsonError(f"object keys must be strings, got {type(key)!r}")
        return "{" + ",".join(f"{_escape(k)}:{_serialize(v)}" for k, v in members) + "}"
    raise CanonicalJsonError(f"unsupported JSON type: {type(value)!r}")


def canonical_json(value: object) -> bytes:
    """Return the RFC 8785 canonical UTF-8 encoding of ``value``."""
    return _serialize(value).encode("utf-8")


def canonical_sha256(value: object) -> str:
    """Return the lowercase hex SHA-256 of the canonical encoding of ``value``."""
    return hashlib.sha256(canonical_json(value)).hexdigest()
