"""Doctor Pet action handlers.

These are the backend side effects behind the desktop Pet buttons. They stay
local and use existing host adapters; no live host config is edited.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PetActionResult:
    delivered: bool
    mode: str
    detail: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def send_recovery_from_status_file(status_file: Path) -> PetActionResult:
    payload = _read_status(status_file)
    platform = str(payload.get("platform") or "generic")
    if platform not in {"openclaw", "hermes"}:
        return PetActionResult(
            delivered=False,
            mode="unsupported",
            detail=f"Doctor Pet recovery delivery only supports OpenClaw/Hermes, got {platform}.",
        )

    source = _first_evidence_file(payload)
    if not source or source == "<manual>":
        return PetActionResult(
            delivered=False,
            mode="manual",
            detail="This incident was manually reported and has no transcript path to route back to.",
        )

    prompt = str(payload.get("recovery_prompt") or "").strip()
    if not prompt:
        prompt = _fallback_recovery_prompt(payload)
    if not prompt:
        return PetActionResult(
            delivered=False,
            mode="empty",
            detail="No recovery prompt was available for this incident.",
        )

    if platform == "openclaw":
        return _send_openclaw_recovery(Path(source).expanduser(), prompt, payload)
    return _send_hermes_recovery(Path(source).expanduser(), prompt, payload)


def _read_status(path: Path) -> dict[str, Any]:
    data = json.loads(path.expanduser().read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Doctor Pet status must be a JSON object.")
    return data


def _first_evidence_file(payload: dict[str, Any]) -> str:
    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return ""
    first = evidence[0]
    if not isinstance(first, dict):
        return ""
    return str(first.get("file") or "")


def _fallback_recovery_prompt(payload: dict[str, Any]) -> str:
    evidence = payload.get("evidence")
    quote = ""
    if isinstance(evidence, list) and evidence and isinstance(evidence[0], dict):
        quote = str(evidence[0].get("quote") or "")
    recommendation = str(payload.get("recommendation") or payload.get("message") or "")
    parts = [
        "Agent Doctor detected a live quality issue.",
        "",
        "Evidence:",
        quote,
        "",
        "Suggested response behavior:",
        recommendation,
    ]
    return "\n".join(part for part in parts if part is not None).strip()


def _send_openclaw_recovery(
    transcript_path: Path,
    prompt: str,
    payload: dict[str, Any],
) -> PetActionResult:
    from .adapters import MessageKind, OpenClawAdapter
    from .channel_router import resolve

    adapter = OpenClawAdapter.detect()
    if adapter is None:
        return PetActionResult(
            delivered=False,
            mode="openclaw_missing",
            detail="OpenClaw was not detected on this machine.",
        )

    target, _language = resolve(transcript_path, adapter)
    caps = adapter.capabilities()
    if target.kind() == "tui" and caps.can_inject_system_event:
        adapter.inject_system_event(prompt, mode="now")
        return PetActionResult(
            delivered=True,
            mode="openclaw_system_event",
            detail="Sent recovery suggestion to the active OpenClaw TUI session.",
        )

    body = _message_body(prompt, payload)
    message_id = adapter.send_message(target, body, MessageKind.intervene)
    return PetActionResult(
        delivered=True,
        mode=f"openclaw_{target.kind()}",
        detail=f"Sent recovery suggestion through OpenClaw adapter ({message_id}).",
    )


def _send_hermes_recovery(
    transcript_path: Path,
    prompt: str,
    payload: dict[str, Any],
) -> PetActionResult:
    from .adapters import HermesAdapter, MessageKind
    from .channel_router import resolve

    adapter = HermesAdapter.detect()
    if adapter is None:
        return PetActionResult(
            delivered=False,
            mode="hermes_missing",
            detail="Hermes was not detected on this machine.",
        )
    target, _language = resolve(transcript_path, adapter)
    message_id = adapter.send_message(target, _message_body(prompt, payload), MessageKind.intervene)
    return PetActionResult(
        delivered=True,
        mode=f"hermes_{target.kind()}",
        detail=f"Wrote recovery suggestion for Hermes ({message_id}).",
    )


def _message_body(prompt: str, payload: dict[str, Any]):
    from .adapters import MessageBody

    session_id = str(payload.get("session_id") or "current session")
    card_path = str(payload.get("card_path") or "")
    footer = f"Session: {session_id}"
    if card_path:
        footer += f"\nCard: {card_path}"
    return MessageBody(
        header="Agent Doctor recovery suggestion",
        body=prompt,
        footer=footer,
    )
