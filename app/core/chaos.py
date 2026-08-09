"""Dev-only latency and fault injection.

Chaos runs before the endpoint and never inside a commit, so an injected fault can delay
or fail a request but cannot leave the store half-written.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from app.core.errors import ChaosInjectedError

#: Health is excluded by default so a chaos run cannot make the service look dead.
DEFAULT_EXCLUDED_PATHS: tuple[str, ...] = ("/api/v1/health",)


@dataclass(slots=True)
class ChaosConfig:
    """Current injection settings. All fields default to "no chaos"."""

    latency_ms: int = 0
    fail_next: int = 0
    error_status: int = 503
    error_code: str = "chaos.injected"
    excluded_paths: tuple[str, ...] = field(default=DEFAULT_EXCLUDED_PATHS)

    def as_dict(self) -> dict[str, object]:
        return {
            "latencyMs": self.latency_ms,
            "failNext": self.fail_next,
            "errorStatus": self.error_status,
            "errorCode": self.error_code,
            "excludedPaths": list(self.excluded_paths),
        }


class ChaosController:
    """Holds mutable chaos state; only the dev routes may change it."""

    def __init__(self) -> None:
        self._config = ChaosConfig()

    @property
    def config(self) -> ChaosConfig:
        return self._config

    def configure(
        self,
        *,
        latency_ms: int,
        fail_next: int,
        error_status: int,
        error_code: str,
        excluded_paths: tuple[str, ...] | None = None,
    ) -> ChaosConfig:
        self._config = ChaosConfig(
            latency_ms=latency_ms,
            fail_next=fail_next,
            error_status=error_status,
            error_code=error_code,
            excluded_paths=excluded_paths if excluded_paths is not None else DEFAULT_EXCLUDED_PATHS,
        )
        return self._config

    def clear(self) -> ChaosConfig:
        self._config = ChaosConfig()
        return self._config

    async def apply(self, path: str) -> None:
        """Delay and/or fail the current request according to the active configuration."""
        config = self._config
        if path in config.excluded_paths:
            return
        if config.latency_ms > 0:
            await asyncio.sleep(config.latency_ms / 1000)
        if config.fail_next > 0:
            self._config = ChaosConfig(
                latency_ms=config.latency_ms,
                fail_next=config.fail_next - 1,
                error_status=config.error_status,
                error_code=config.error_code,
                excluded_paths=config.excluded_paths,
            )
            raise ChaosInjectedError(
                "deliberate dev-only fault injected by chaos controls",
                code=config.error_code,
                status_code=config.error_status,
                details={"path": path},
            )
