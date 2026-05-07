"""Agent Doctor action handlers.

These are the backend side effects behind the desktop Agent Doctor buttons.
They stay local and use existing host adapters; no live host config is edited.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
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
    if platform != "openclaw":
        return PetActionResult(
            delivered=False,
            mode="unsupported",
            detail=f"Tell Current Agent is v1 OpenClaw-only; got {platform}.",
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

    return _send_openclaw_recovery(Path(source).expanduser(), prompt, payload)


def diagnose_current_from_status_file(status_file: Path) -> PetActionResult:
    payload = _read_status_if_present(status_file)
    platform, transcript_path = _current_transcript_target(payload)
    if transcript_path is None:
        return PetActionResult(
            delivered=False,
            mode="no_supported_session",
            detail="No OpenClaw or Hermes transcript directory was found.",
        )

    from .ingest import IngestError
    from .pet import pet_status_for_path, write_pet_artifacts

    try:
        status = pet_status_for_path(transcript_path, platform=platform)
    except IngestError as exc:
        return PetActionResult(
            delivered=False,
            mode=f"{platform}_unreadable",
            detail=str(exc),
        )
    if status.state in {"concerned", "intervening"}:
        detail = "Checked the current session and found a quality incident."
    else:
        detail = "Current session checked. No active quality signal was found."
        status = replace(
            status,
            headline="Current session checked.",
            message=(
                "Agent Doctor checked the latest OpenClaw/Hermes transcript and found "
                "no active frustration or quality signal."
            ),
            diagnosis=(
                "No active user-frustration, hidden tool-failure, or unverified-success "
                "signal was found in the latest supported session."
            ),
            recommendation=(
                "Keep working normally. Agent Doctor is still watching supported "
                "OpenClaw/Hermes sessions."
            ),
            recovery_prompt="",
        )
    write_pet_artifacts(status_file.expanduser().parent, status)
    return PetActionResult(
        delivered=True,
        mode=f"{platform}_diagnosed",
        detail=detail,
    )


def _read_status(path: Path) -> dict[str, Any]:
    data = json.loads(path.expanduser().read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Agent Doctor status must be a JSON object.")
    return data


def _read_status_if_present(path: Path) -> dict[str, Any]:
    try:
        return _read_status(path)
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}


def _current_transcript_target(payload: dict[str, Any]) -> tuple[str, Path | None]:
    from .ingest import DEFAULT_HERMES_PATH, DEFAULT_OPENCLAW_PATH

    platform = str(payload.get("platform") or "").strip().casefold()
    evidence_file = _first_evidence_file(payload)
    if evidence_file and evidence_file != "<manual>":
        path = Path(evidence_file).expanduser()
        if path.exists() and platform in {"openclaw", "hermes"}:
            return (platform, path)
    if platform == "openclaw" and DEFAULT_OPENCLAW_PATH.exists():
        return ("openclaw", _latest_transcript_path(DEFAULT_OPENCLAW_PATH))
    if platform == "hermes" and DEFAULT_HERMES_PATH.exists():
        return ("hermes", _latest_transcript_path(DEFAULT_HERMES_PATH))
    if DEFAULT_OPENCLAW_PATH.exists():
        return ("openclaw", _latest_transcript_path(DEFAULT_OPENCLAW_PATH))
    if DEFAULT_HERMES_PATH.exists():
        return ("hermes", _latest_transcript_path(DEFAULT_HERMES_PATH))
    return ("generic", None)


def _latest_transcript_path(root: Path) -> Path | None:
    candidates = [path for path in root.rglob("*.jsonl") if path.is_file()]
    ordinary = [path for path in candidates if not path.name.endswith(".trajectory.jsonl")]
    selected = ordinary or candidates
    if not selected:
        return None
    return max(selected, key=lambda path: path.stat().st_mtime)


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
    from .adapters import OpenClawAdapter
    from .channel_router import resolve

    adapter = OpenClawAdapter.detect()
    if adapter is None:
        return PetActionResult(
            delivered=False,
            mode="openclaw_missing",
            detail="OpenClaw was not detected; Agent Doctor could not route Tell Current Agent.",
        )

    try:
        target, _language = resolve(transcript_path, adapter)
        caps = adapter.capabilities()
    except Exception as exc:
        return PetActionResult(
            delivered=False,
            mode="openclaw_route_failed",
            detail=f"Could not resolve the current OpenClaw session: {exc}",
        )

    if target.kind() != "tui":
        return PetActionResult(
            delivered=False,
            mode="openclaw_not_routable",
            detail="The incident is not routable to an active OpenClaw TUI session.",
        )

    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        return PetActionResult(
            delivered=False,
            mode="openclaw_session_missing",
            detail="The incident did not include an OpenClaw session id for targeted delivery.",
        )

    send_agent_turn = getattr(adapter, "send_agent_turn", None)
    if callable(send_agent_turn):
        try:
            send_agent_turn(session_id, prompt)
        except Exception as exc:
            return PetActionResult(
                delivered=False,
                mode="openclaw_agent_session_failed",
                detail=f"OpenClaw targeted session delivery failed: {exc}",
            )

        return PetActionResult(
            delivered=True,
            mode="openclaw_agent_session",
            detail=f"Tell Current Agent sent the structured intervention to OpenClaw session {session_id}.",
        )

    if not caps.can_inject_system_event:
        return PetActionResult(
            delivered=False,
            mode="openclaw_not_routable",
            detail="The OpenClaw adapter cannot deliver a targeted agent turn or system event.",
        )

    try:
        adapter.inject_system_event(prompt, mode="now")
    except Exception as exc:
        return PetActionResult(
            delivered=False,
            mode="openclaw_system_event_failed",
            detail=f"OpenClaw system-event delivery failed: {exc}",
        )

    return PetActionResult(
        delivered=True,
        mode="openclaw_system_event",
        detail="Tell Current Agent injected the structured intervention into the active OpenClaw system event stream.",
    )
