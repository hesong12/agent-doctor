"""GenericAdapter: file-inbox + OS-notification fallback for any host.

This is the always-available adapter. It does not require OpenClaw,
Hermes, or any specific binary. It is what downstream code falls back
to when the user's host can't send into a real channel.

Capabilities are intentionally minimal — every flag is False except
for what's covered by `send_message` (inbox-file write).
"""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import time
import uuid
from pathlib import Path

from .base import (
    HostAdapter,
    HostCapabilities,
    MessageBody,
    MessageKind,
    Reaction,
    SessionMetadata,
    Target,
)


class GenericAdapter:
    """Generic / fallback adapter. No host-specific CLI required."""

    @classmethod
    def detect(cls) -> "GenericAdapter":
        """Always present; returns an instance."""
        return cls()

    def capabilities(self) -> HostCapabilities:
        return HostCapabilities(host_name="generic", detected_at=Path("/"))

    def send_message(
        self,
        target: Target,
        body: MessageBody,
        kind: MessageKind,
    ) -> str:
        """Write the message to target.inbox_path. OS notification on best-effort.

        Returns a synthetic message_id for tracking in messages.jsonl.
        """
        if target.inbox_path is None:
            raise NotImplementedError(
                "GenericAdapter.send_message requires Target.inbox_path; "
                "GenericAdapter has can_send_message=False."
            )
        inbox = target.inbox_path.expanduser()
        inbox.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        inbox.write_text(body.render() + "\n", encoding="utf-8")
        try:
            inbox.chmod(0o600)
        except OSError:
            pass
        self._best_effort_os_notification(body)
        return f"generic:{uuid.uuid4().hex[:12]}:{int(time.time())}"

    def edit_message(self, target: Target, message_id: str, body: MessageBody) -> None:
        """Edit an inbox-file message: rewrite the file with the new body."""
        if target.inbox_path is None:
            raise NotImplementedError(
                "GenericAdapter.edit_message requires Target.inbox_path."
            )
        inbox = target.inbox_path.expanduser()
        inbox.write_text(body.render() + "\n", encoding="utf-8")

    def add_reaction(self, target: Target, message_id: str, emoji: str) -> None:
        """Generic has no reaction surface."""
        # No-op; capability flag is False so callers shouldn't reach here.

    def list_reactions(self, target: Target, message_id: str) -> list[Reaction]:
        """Generic has no reaction surface."""
        return []

    def inject_system_event(self, text: str, *, mode: str = "now") -> None:
        """Generic has no system-event surface."""
        # No-op; capability flag is False.

    def infer_text(self, prompt: str, *, model: str | None = None) -> str:
        raise NotImplementedError(
            "GenericAdapter has can_infer_text=False; use a host-specific adapter."
        )

    def infer_embedding(self, text: str, *, model: str | None = None) -> list[float]:
        raise NotImplementedError(
            "GenericAdapter has can_infer_embedding=False; use a host-specific adapter."
        )

    def session_metadata(self, jsonl_path: Path) -> SessionMetadata:
        """Best-effort metadata: session_id from first JSONL line or filename;
        language detected from the dominant CJK/Latin character class in
        the first ~1000 chars; channel/recipient defaulted to generic.
        """
        session_id = jsonl_path.expanduser().stem
        language = "en"
        try:
            sample = jsonl_path.expanduser().read_text(encoding="utf-8", errors="replace")[:4000]
            try:
                first = json.loads(sample.splitlines()[0]) if sample.splitlines() else {}
                if isinstance(first, dict) and first.get("session_id"):
                    session_id = str(first["session_id"])
            except (json.JSONDecodeError, IndexError):
                pass
            language = self._detect_language(sample)
        except OSError:
            pass
        return SessionMetadata(
            session_id=session_id,
            language=language,
            channel="generic",
            recipient="local",
        )

    @staticmethod
    def _detect_language(sample: str) -> str:
        """Crude majority detection. CJK > Latin → 'zh'; else 'en'."""
        cjk = len(re.findall(r"[一-鿿]", sample))
        latin = len(re.findall(r"[A-Za-z]", sample))
        if cjk > latin and cjk > 20:
            return "zh"
        return "en"

    @staticmethod
    def _best_effort_os_notification(body: MessageBody) -> None:
        """macOS osascript / Linux notify-send. Failures are silent —
        capability flag is for in-channel delivery, not OS notification.
        """
        title = body.header[:120]
        message = body.body[:240]
        if platform.system() == "Darwin":
            try:
                subprocess.run(
                    ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
                    capture_output=True,
                    timeout=5,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        elif platform.system() == "Linux":
            try:
                subprocess.run(
                    ["notify-send", title, message],
                    capture_output=True,
                    timeout=5,
                )
            except (OSError, subprocess.SubprocessError):
                pass
