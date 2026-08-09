"""Storage namespace derivation, frozen so Dart and Python agree byte for byte.

The Flutter client keeps one database per data origin. Getting the namespace wrong would
either mix two servers' data into one database or silently orphan a user's queued
commands, so the rule is specified here and exported as vectors rather than reimplemented
independently on each side.

``fixture`` is the literal ``fixture:v<contract major>``. ``remote`` is
``remote:<sha256(RFC8785([normalizedFullBaseUrl, apiMajor, schemaMajor]))>``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

from app.core.canonical_json import canonical_json

DEFAULT_PORTS: Final[dict[str, int]] = {"http": 80, "https": 443}


class NamespaceError(ValueError):
    """The base URL cannot produce a stable namespace."""


@dataclass(frozen=True, slots=True)
class NormalizedBaseUrl:
    scheme: str
    host: str
    port: int
    path: str

    def as_text(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}{self.path}"


def normalize_base_url(raw: str) -> NormalizedBaseUrl:
    """Normalise scheme/host case, insert the effective port and strip a trailing slash.

    Query strings, fragments and user-info are rejected outright: a namespace derived from
    them would change whenever an unrelated request parameter changed.
    """
    candidate = raw.strip()
    if not candidate:
        raise NamespaceError("base URL is empty")
    parts = urlsplit(candidate)
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS:
        raise NamespaceError(f"unsupported scheme: {parts.scheme!r}")
    if parts.query:
        raise NamespaceError("base URL must not contain a query string")
    if parts.fragment:
        raise NamespaceError("base URL must not contain a fragment")
    if parts.username or parts.password:
        raise NamespaceError("base URL must not contain user information")
    if not parts.hostname:
        raise NamespaceError("base URL must contain a host")

    host = parts.hostname.lower()
    try:
        # IDNA keeps Unicode hosts stable across platforms; ASCII hosts are unchanged.
        host = host.encode("idna").decode("ascii") if not _is_ip_literal(host) else host
    except UnicodeError as exc:
        raise NamespaceError(f"host cannot be encoded: {parts.hostname!r}") from exc

    port = parts.port if parts.port is not None else DEFAULT_PORTS[scheme]
    path = parts.path or ""
    while path.endswith("/") and path != "/":
        path = path[:-1]
    if path == "/":
        path = ""
    if path and not path.startswith("/"):
        path = "/" + path
    return NormalizedBaseUrl(scheme=scheme, host=host, port=port, path=path)


def _is_ip_literal(host: str) -> bool:
    return ":" in host or all(part.isdigit() for part in host.split(".") if part)


def fixture_namespace(contract_major: str) -> str:
    return f"fixture:v{contract_major}"


def remote_namespace(*, base_url: str, api_major: str, schema_major: str) -> str:
    normalized = normalize_base_url(base_url)
    payload = [normalized.as_text(), api_major, schema_major]
    digest = hashlib.sha256(canonical_json(payload)).hexdigest()
    return f"remote:{digest}"


def database_name(namespace: str) -> str:
    """Physical database name on native and web."""
    return f"kxinspections_{hashlib.sha256(namespace.encode('utf-8')).hexdigest()}"
