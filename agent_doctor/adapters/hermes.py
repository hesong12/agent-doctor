"""HermesAdapter (stub): detects Hermes, declares partial capabilities.

Hermes's outbound surface (message send, reactions API, system-event
equivalent, inference CLI) is not yet implemented in this adapter.
For now the adapter:
  - detects ~/.hermes existence
  - declares skill_dir so install/bootstrap can write SKILL.md
  - declares all outbound capabilities False
  - falls send_message through to inbox-file via GenericAdapter so
    users still see something

Phase 5 in the spec extends this once Hermes outbound CLI is identified.
Community PRs welcome — see docs/adapters/hermes.md.
"""
from __future__ import annotations

from pathlib import Path

from .base import (
    HostCapabilities,
    MessageBody,
    MessageKind,
    Reaction,
    SessionMetadata,
    Target,
)
from .generic import GenericAdapter

HERMES_HOME = Path("~/.hermes").expanduser()


class HermesAdapter:
    @classmethod
    def detect(cls) -> "HermesAdapter | None":
        if not HERMES_HOME.exists():
            return None
        return cls()

    def capabilities(self) -> HostCapabilities:
        return HostCapabilities(
            host_name="hermes",
            detected_at=HERMES_HOME,
            skill_dir=HERMES_HOME / "skills" / "autonomous-ai-agents" / "agent-doctor",
            memory_writable=HERMES_HOME / "memory" / "MEMORY.md",
            identity_writable=HERMES_HOME / "identity" / "identity.md",
            can_write_inbox=True,
            available_channels=("inbox",),
            # All outbound flags default to False — Hermes outbound surface TBD.
        )

    def send_message(self, target: Target, body: MessageBody, kind: MessageKind) -> str:
        return GenericAdapter().send_message(target, body, kind)

    def edit_message(self, target: Target, message_id: str, body: MessageBody) -> None:
        GenericAdapter().edit_message(target, message_id, body)

    def add_reaction(self, target: Target, message_id: str, emoji: str) -> None:
        # No reaction surface yet.
        pass

    def list_reactions(self, target: Target, message_id: str) -> list[Reaction]:
        return []

    def inject_system_event(self, text: str, *, mode: str = "now") -> None:
        # No system-event equivalent yet.
        pass

    def infer_text(self, prompt: str, *, model: str | None = None) -> str:
        raise NotImplementedError(
            "HermesAdapter has can_infer_text=False; outbound CLI not implemented yet."
        )

    def infer_embedding(self, text: str, *, model: str | None = None) -> list[float]:
        raise NotImplementedError(
            "HermesAdapter has can_infer_embedding=False; outbound CLI not implemented yet."
        )

    def session_metadata(self, jsonl_path: Path) -> SessionMetadata:
        return GenericAdapter().session_metadata(jsonl_path)

    def install_skill(self, content: str, *, dry_run: bool = False) -> Path:
        skill_dir = self.capabilities().skill_dir
        assert skill_dir is not None, "Hermes declares skill_dir; missing capability is a bug"
        skill_path = skill_dir / "SKILL.md"
        if dry_run:
            return skill_path
        skill_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        skill_path.write_text(content, encoding="utf-8")
        skill_path.chmod(0o600)
        return skill_path
