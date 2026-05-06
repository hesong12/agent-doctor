"""Host adapters: pluggable per-host CLI/API integrations.

Every memoryful agent framework agent-doctor supports (OpenClaw, Hermes,
generic) is served by a HostAdapter implementing a published Protocol.
The adapter declares its HostCapabilities so downstream code can pick
the best available delivery / inference path or degrade gracefully.

Public API:
    HostAdapter      — Protocol every adapter implements
    HostCapabilities — dataclass declaring what a host supports
    Target           — outbound message destination (channel + recipient)
    MessageBody      — structured outbound message
    MessageKind      — enum: intervene / propose / digest / applied / undone
    Reaction         — inbound reaction on one of our messages
    SessionMetadata  — what we learn from a session JSONL header

    GenericAdapter   — always-available file-inbox fallback
"""

from .base import (
    HostAdapter,
    HostCapabilities,
    Target,
    MessageBody,
    MessageKind,
    Reaction,
    SessionMetadata,
)
from .generic import GenericAdapter
from .hermes import HermesAdapter
from .openclaw import OpenClawAdapter

__all__ = [
    "HostAdapter",
    "HostCapabilities",
    "Target",
    "MessageBody",
    "MessageKind",
    "Reaction",
    "SessionMetadata",
    "GenericAdapter",
    "HermesAdapter",
    "OpenClawAdapter",
]
