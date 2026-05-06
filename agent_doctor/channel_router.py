"""Channel router: session JSONL → outbound Target.

Asks the host adapter for session_metadata, then constructs a Target
that the speaker + adapter.send_message can use. For TUI / inbox-only
sessions, sets `inbox_path` so GenericAdapter fallback writes a file.

Returns (Target, language) so callers can pass language to the speaker
for localized templates.
"""
from __future__ import annotations

from pathlib import Path
from typing import Tuple

from .adapters import HostAdapter, Target


def resolve(jsonl_path: Path, adapter: HostAdapter) -> Tuple[Target, str]:
    """Resolve a session's outbound Target + language.

    Reads session metadata via the adapter (which knows host-specific
    JSONL shapes). For channel-based sessions (telegram/discord/etc.),
    constructs Target with the actual channel + recipient. For TUI
    sessions, sets inbox_path so GenericAdapter inbox fallback fires.
    """
    metadata = adapter.session_metadata(jsonl_path)
    caps = adapter.capabilities()
    host = caps.host_name

    inbox_root = Path("~/.agent-doctor").expanduser() / host / "inbox"
    inbox_path = inbox_root / f"{metadata.session_id}.md"

    # TUI sessions get inbox + OS notification fallback only
    if metadata.channel == "tui":
        return (
            Target(
                host=host,
                channel="tui",
                recipient=metadata.recipient or "local",
                inbox_path=inbox_path,
            ),
            metadata.language,
        )

    # Real channel-based session
    if metadata.recipient:
        return (
            Target(
                host=host,
                channel=metadata.channel,
                recipient=metadata.recipient,
                inbox_path=inbox_path,  # secondary fallback
            ),
            metadata.language,
        )

    # No real channel info; fall through to inbox-only
    return (
        Target(
            host=host,
            channel="inbox",
            recipient="",
            inbox_path=inbox_path,
        ),
        metadata.language,
    )
