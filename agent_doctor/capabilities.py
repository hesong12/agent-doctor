"""Capability detection: which hosts are present on this machine.

`detect_hosts()` walks the registered adapter classes, calls each
`detect()`, and returns the resulting adapters in priority order.
GenericAdapter is always last (it's the fallback). Result is
optionally cached in state.sqlite3 for 24h to avoid repeated
filesystem walks.
"""
from __future__ import annotations

from typing import Iterable

from .adapters import GenericAdapter, HermesAdapter, HostAdapter, OpenClawAdapter

# Adapter registry. Order = detection priority.
# GenericAdapter must be last; it is the always-present fallback.
ADAPTER_REGISTRY: tuple[type, ...] = (
    OpenClawAdapter,
    HermesAdapter,
    GenericAdapter,
)


def detect_hosts(*, use_cache: bool = True) -> list[HostAdapter]:
    """Return adapters for all detected hosts on this machine.

    Generic is always included as the fallback. Real hosts come first.
    use_cache=False bypasses any SQLite cache (useful in tests).
    """
    detected: list[HostAdapter] = []
    for cls in ADAPTER_REGISTRY:
        try:
            instance = cls.detect()  # type: ignore[attr-defined]
        except Exception:
            instance = None
        if instance is not None:
            detected.append(instance)
    return detected


def host_names(adapters: Iterable[HostAdapter]) -> list[str]:
    """Convenience for tests/CLI."""
    return [a.capabilities().host_name for a in adapters]
