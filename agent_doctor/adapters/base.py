"""Adapter base types: Protocol, dataclasses, enums.

Every host (OpenClaw, Hermes, generic) implements HostAdapter and
declares its HostCapabilities. Downstream code branches on capabilities,
not on host identity, so adding a new host is one new file plus one
registration entry.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable, Literal, Protocol, runtime_checkable


class MessageKind(Enum):
    intervene = "intervene"
    propose = "propose"
    digest = "digest"
    applied = "applied"
    undone = "undone"


@dataclass(frozen=True)
class Target:
    """Outbound destination resolved from a session.

    For channel-based hosts (OpenClaw + Telegram/Discord/etc.), `channel`
    is e.g. "telegram" and `recipient` is the chat id / handle. For
    TUI-only sessions, channel="tui" and inbox_path points at the
    fallback advisory file.
    """
    host: str
    channel: str
    recipient: str
    inbox_path: Path | None = None

    def kind(self) -> Literal["channel", "tui", "inbox"]:
        if self.channel == "tui":
            return "tui"
        if self.inbox_path is not None and not self.recipient:
            return "inbox"
        return "channel"


@dataclass(frozen=True)
class MessageBody:
    """Structured outbound message rendered by `speaker.py`.

    Header, body, optional footer (CLI fallback hint, undo command, etc.).
    `render()` is deliberately string-concatenating; channel-specific
    formatting (markdown, HTML) is the adapter's job, not the speaker's.
    """
    header: str
    body: str
    footer: str | None = None

    def __post_init__(self) -> None:
        # A buggy speaker should not be able to write blank intervention
        # messages to a user's inbox. Validate at construction time.
        if not self.header:
            raise ValueError("MessageBody.header must be non-empty")
        if not self.body:
            raise ValueError("MessageBody.body must be non-empty")

    def render(self) -> str:
        parts = [self.header, "", self.body]
        if self.footer:
            parts.extend(["", self.footer])
        return "\n".join(parts)


@dataclass(frozen=True)
class Reaction:
    message_id: str
    emoji: str
    user_id: str
    at: float


@dataclass(frozen=True)
class SessionMetadata:
    """What the channel router learns from a session's JSONL header."""
    session_id: str
    language: str  # ISO 639-1 short code, "en" / "zh" / "ja" / etc.
    channel: str
    recipient: str


@dataclass(frozen=True)
class HostCapabilities:
    """Everything an adapter declares about its host.

    Defaults are conservative: a new adapter that overrides nothing is
    treated as having no capabilities, which forces the downstream code
    paths to degrade. Adapters override flags to True only when they
    have actually implemented and verified the corresponding method.
    """
    host_name: str
    detected_at: Path

    can_send_message: bool = False
    can_edit_message: bool = False
    can_react: bool = False
    can_list_reactions: bool = False
    can_inject_system_event: bool = False
    can_infer_text: bool = False
    can_infer_embedding: bool = False

    default_inference_model: str | None = None
    available_models: tuple[str, ...] = ()
    available_channels: tuple[str, ...] = ()

    skill_dir: Path | None = None
    memory_writable: Path | None = None
    identity_writable: Path | None = None
    sop_writable: Path | None = None

    def __post_init__(self) -> None:
        # Coerce list/iterable inputs to tuple so frozen-dataclass invariants
        # hold for adapter authors who pass list-shaped values. Without this,
        # downstream code like `caps.available_models + ("foo",)` would
        # TypeError at the call site instead of failing loud at construction.
        if not isinstance(self.available_models, tuple):
            object.__setattr__(self, "available_models", tuple(self.available_models))
        if not isinstance(self.available_channels, tuple):
            object.__setattr__(self, "available_channels", tuple(self.available_channels))


@runtime_checkable
class HostAdapter(Protocol):
    """Adapter Protocol every host implements.

    Methods that aren't supported by a host should still be defined but
    raise NotImplementedError or no-op consistent with their declared
    capabilities. Consumers must check `capabilities()` before calling.
    """

    @classmethod
    def detect(cls) -> "HostAdapter | None":
        """Return an adapter instance if the host is present on this machine, else None."""
        ...

    def capabilities(self) -> HostCapabilities:
        """Return the capability matrix for this host."""
        ...

    def send_message(self, target: Target, body: MessageBody, kind: MessageKind) -> str:
        """Post a message; return an opaque message_id for later edit/react/list-reactions."""
        ...

    def edit_message(self, target: Target, message_id: str, body: MessageBody) -> None:
        """Edit an existing message (e.g., to mark it applied/expired)."""
        ...

    def add_reaction(self, target: Target, message_id: str, emoji: str) -> None:
        """Attach a reaction to a message we posted (used to seed ✅/❌/💬)."""
        ...

    def list_reactions(self, target: Target, message_id: str) -> list[Reaction]:
        """List user reactions on one of our messages."""
        ...

    def inject_system_event(self, text: str, *, mode: str = "now") -> None:
        """Wake the host agent with a system event (e.g. openclaw system event)."""
        ...

    def infer_text(self, prompt: str, *, model: str | None = None) -> str:
        """Run text inference using the host's configured provider."""
        ...

    def infer_embedding(self, text: str, *, model: str | None = None) -> list[float]:
        """Run an embedding via the host's configured provider."""
        ...

    def session_metadata(self, jsonl_path: Path) -> SessionMetadata:
        """Parse a session's JSONL header to resolve channel + recipient + language."""
        ...

    def install_skill(self, content: str, *, dry_run: bool = False) -> Path:
        """Write the SKILL.md content into the host's skill directory.

        Returns the absolute path written. dry_run=True returns the path
        that would be written without actually writing. Adapters that
        don't have a skill_dir capability raise NotImplementedError.
        """
        ...
