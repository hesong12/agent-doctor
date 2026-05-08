"""OpenClawAdapter: full HostAdapter for OpenClaw.

Wraps the public `openclaw` CLI:
  - openclaw message send / edit / react / reactions list
  - openclaw system event
  - openclaw infer model run
  - openclaw infer embedding create

Reuses the Phase 0 fix from `agent_doctor.delivery`:
  - `resolve_openclaw_binary` finds openclaw under launchd's minimal PATH.
  - `_openclaw_subprocess_env` augments PATH so downstream calls work.

Capability flags reflect what's reachable: when the openclaw binary is
not on PATH, all capability flags are False so downstream code degrades
gracefully (typically falling through to GenericAdapter inbox).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

from agent_doctor.delivery import (
    _openclaw_subprocess_env,
    resolve_openclaw_binary,
)

from .base import (
    HostCapabilities,
    MessageBody,
    MessageKind,
    Reaction,
    SessionMetadata,
    Target,
)
from .generic import GenericAdapter

_log = logging.getLogger(__name__)

OPENCLAW_HOME = Path("~/.openclaw").expanduser()

# OpenClaw sessionKey scope markers that indicate a local session
# (no external channel surface; route to TUI/inbox).
_LOCAL_SCOPES = frozenset({"main", "tui", "cron", "subagent", "explicit"})


def _classify_session_scope(session_key: str) -> str:
    """Map an OpenClaw sessionKey to a channel slug.

    Returns 'tui' for any local scope, or the external channel slug
    (e.g. 'telegram') otherwise. Channel slug is the leading component
    of the third sessionKey field, before any '-' or ':' suffix.
    """
    parts = session_key.split(":")
    if len(parts) < 3:
        return "tui"  # safest default for unrecognized shapes
    third = parts[2]
    # tui-<UUID> and tui both classify as local TUI
    if third.startswith("tui-") or third == "tui":
        return "tui"
    if third in _LOCAL_SCOPES:
        return "tui"
    # External channel: parse slug before first '-' (e.g. 'telegram-12345' → 'telegram')
    return third.split("-", 1)[0] if "-" in third else third


def _resolve_openclaw_or_none() -> str | None:
    try:
        return resolve_openclaw_binary("openclaw", env=os.environ)
    except RuntimeError:
        return None


def _run_openclaw(
    args: list[str],
    *,
    timeout: float = 30,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an openclaw subcommand. Returns CompletedProcess; caller checks rc."""
    binary = _resolve_openclaw_or_none()
    if binary is None:
        raise RuntimeError("openclaw binary not found")
    cmd = [binary] + args
    env = _openclaw_subprocess_env(extra_env or {})
    return subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
    )


class OpenClawAdapter:
    """HostAdapter for OpenClaw."""

    def __init__(self) -> None:
        self._channels_cache: tuple[str, ...] | None = None

    @classmethod
    def detect(cls) -> "OpenClawAdapter | None":
        if not OPENCLAW_HOME.exists():
            return None
        return cls()

    def capabilities(self) -> HostCapabilities:
        binary = _resolve_openclaw_or_none()
        has_binary = binary is not None
        return HostCapabilities(
            host_name="openclaw",
            detected_at=OPENCLAW_HOME,
            can_send_message=has_binary,
            can_edit_message=has_binary,
            can_react=has_binary,
            can_list_reactions=has_binary,
            can_inject_system_event=has_binary,
            can_infer_text=has_binary,
            can_infer_embedding=has_binary,
            default_inference_model=None,  # use host's configured default
            available_models=(),  # populated lazily on first list_models() call
            available_channels=self._channels() if has_binary else (),
            skill_dir=OPENCLAW_HOME / "skills" / "agent-doctor",
            memory_writable=OPENCLAW_HOME / "memory" / "MEMORY.md",
            identity_writable=OPENCLAW_HOME / "identity" / "identity.md",
            sop_writable=None,  # SOP lives inside skills/agent-doctor/SKILL.md, edited there
        )

    def _channels(self) -> tuple[str, ...]:
        """Cached wrapper around `_discover_channels()`. capabilities() is
        called by the autopilot watch loop and contract tests; without
        caching, each call shells out to `openclaw channels list --json`
        with up to a 10s timeout."""
        if self._channels_cache is None:
            self._channels_cache = self._discover_channels()
        return self._channels_cache

    def send_message(self, target: Target, body: MessageBody, kind: MessageKind) -> str:
        # TUI sessions and inbox-only targets have no real channel surface;
        # fall through to GenericAdapter so the user still sees the message
        # in their advisory file. Also fall through if the binary is missing
        # but the caller handed us an inbox path — graceful degradation.
        if target.kind() in ("tui", "inbox") or (
            target.inbox_path is not None and _resolve_openclaw_or_none() is None
        ):
            return GenericAdapter().send_message(target, body, kind)
        rendered = body.render()
        args = [
            "message", "send",
            "--channel", target.channel,
            "--target", target.recipient,
            "--message", rendered,
            "--json",
        ]
        result = _run_openclaw(args)
        if result.returncode != 0:
            raise RuntimeError(
                f"openclaw message send failed: rc={result.returncode} "
                f"stderr={result.stderr.strip()!r}"
            )
        try:
            payload = json.loads(result.stdout or "{}")
            return str(payload.get("messageId") or payload.get("id") or "")
        except json.JSONDecodeError:
            return ""

    def edit_message(self, target: Target, message_id: str, body: MessageBody) -> None:
        if target.kind() in ("tui", "inbox") or (
            target.inbox_path is not None and _resolve_openclaw_or_none() is None
        ):
            GenericAdapter().edit_message(target, message_id, body)
            return
        args = [
            "message", "edit",
            "--channel", target.channel,
            "--target", target.recipient,
            "--message-id", message_id,
            "--message", body.render(),
            "--json",
        ]
        result = _run_openclaw(args)
        if result.returncode != 0:
            raise RuntimeError(
                f"openclaw message edit failed: rc={result.returncode} "
                f"stderr={result.stderr.strip()!r}"
            )

    def add_reaction(self, target: Target, message_id: str, emoji: str) -> None:
        args = [
            "message", "react",
            "--channel", target.channel,
            "--target", target.recipient,
            "--message-id", message_id,
            "--emoji", emoji,
        ]
        try:
            result = _run_openclaw(args)  # best effort; callers don't need to wait on rc
            if result.returncode != 0:
                _log.debug(
                    "openclaw message react rc=%s stderr=%r",
                    result.returncode,
                    result.stderr.strip(),
                )
        except RuntimeError as exc:
            # Binary missing or transient failure: reactions are best-effort,
            # but log at DEBUG so opt-in operator debugging works.
            _log.debug("openclaw message react failed: %s", exc)

    def list_reactions(self, target: Target, message_id: str) -> list[Reaction]:
        args = [
            "message", "reactions",
            "--channel", target.channel,
            "--target", target.recipient,
            "--message-id", message_id,
            "--json",
        ]
        try:
            result = _run_openclaw(args)
        except RuntimeError as exc:
            # Binary missing — contract requires returning a list (possibly empty).
            _log.debug("openclaw message reactions failed: %s", exc)
            return []
        if result.returncode != 0:
            _log.debug(
                "openclaw message reactions rc=%s stderr=%r",
                result.returncode,
                result.stderr.strip(),
            )
            return []
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            _log.debug("openclaw message reactions returned invalid JSON")
            return []
        out: list[Reaction] = []
        for item in payload.get("reactions", []):
            out.append(
                Reaction(
                    message_id=str(item.get("messageId", message_id)),
                    emoji=str(item.get("emoji", "")),
                    user_id=str(item.get("userId", "")),
                    at=float(item.get("timestamp", 0.0)),
                )
            )
        return out

    def inject_system_event(self, text: str, *, mode: str = "now") -> None:
        args = [
            "system", "event",
            "--mode", mode,
            "--text", text,
        ]
        result = _run_openclaw(args, timeout=35)
        if result.returncode != 0:
            raise RuntimeError(
                f"openclaw system event failed: rc={result.returncode} "
                f"stderr={result.stderr.strip()!r}"
            )

    def send_agent_turn(self, session_id: str, text: str, *, timeout_seconds: int = 600) -> None:
        cleaned_session_id = session_id.strip()
        if not cleaned_session_id:
            raise RuntimeError("OpenClaw session id is required")
        args = [
            "agent",
            "--session-id", cleaned_session_id,
            "--timeout", str(timeout_seconds),
            "--json",
            "--message", text,
        ]
        result = _run_openclaw(args, timeout=max(35, timeout_seconds + 5))
        if result.returncode != 0:
            raise RuntimeError(
                f"openclaw agent session delivery failed: rc={result.returncode} "
                f"stderr={result.stderr.strip()!r}"
            )
        if not result.stdout.strip():
            raise RuntimeError("openclaw agent returned no output")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("openclaw agent returned non-JSON output") from exc
        status = str(payload.get("status") or "failed").casefold()
        if status not in {"ok", "completed", "success"}:
            detail = payload.get("summary") or payload.get("error") or result.stdout
            raise RuntimeError(f"openclaw agent session delivery did not complete: {detail}")

    def infer_text(self, prompt: str, *, model: str | None = None) -> str:
        # Honor the capability contract: if the binary isn't reachable we
        # cannot infer; raise NotImplementedError so callers (and the
        # AdapterContractTest) see a flag-consistent error rather than a
        # generic runtime failure.
        if _resolve_openclaw_or_none() is None:
            raise NotImplementedError(
                "OpenClawAdapter has can_infer_text=False (openclaw binary not reachable)."
            )
        args = ["infer", "model", "run", "--prompt", prompt, "--json"]
        if model:
            args.extend(["--model", model])
        result = _run_openclaw(args, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(
                f"openclaw infer model run failed: rc={result.returncode} "
                f"stderr={result.stderr.strip()!r}"
            )
        try:
            payload = json.loads(result.stdout or "{}")
            outputs = payload.get("outputs") or []
            if outputs:
                return str(outputs[0].get("text", ""))
            return ""
        except json.JSONDecodeError:
            return ""

    def infer_embedding(self, text: str, *, model: str | None = None) -> list[float]:
        if _resolve_openclaw_or_none() is None:
            raise NotImplementedError(
                "OpenClawAdapter has can_infer_embedding=False (openclaw binary not reachable)."
            )
        args = ["infer", "embedding", "create", "--text", text, "--json"]
        if model:
            args.extend(["--model", model])
        result = _run_openclaw(args, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(
                f"openclaw infer embedding failed: rc={result.returncode} "
                f"stderr={result.stderr.strip()!r}"
            )
        try:
            payload = json.loads(result.stdout or "{}")
            vec = payload.get("embeddings") or payload.get("vector") or []
            if vec and isinstance(vec, list) and isinstance(vec[0], list):
                vec = vec[0]  # some providers return [[...]]
            return [float(x) for x in vec]
        except (json.JSONDecodeError, TypeError, ValueError):
            return []

    def install_skill(self, content: str, *, dry_run: bool = False) -> Path:
        skill_dir = self.capabilities().skill_dir
        assert skill_dir is not None, "OpenClaw declares skill_dir; missing capability is a bug"
        skill_path = skill_dir / "SKILL.md"
        if dry_run:
            return skill_path
        skill_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        skill_path.write_text(content, encoding="utf-8")
        skill_path.chmod(0o600)
        return skill_path

    def session_metadata(self, jsonl_path: Path) -> SessionMetadata:
        """Parse OpenClaw session JSONL: trace files contain sessionKey
        like 'agent:<agent>:<scope>[:...]' where scope is one of:
          - 'main' (default TUI session)
          - 'tui' or 'tui-<UUID>' (named TUI sessions)
          - 'cron:<job>:run:<id>' (scheduled job)
          - 'subagent:<id>' (sub-agent)
          - 'explicit:<name>' (named one-off session)
          - '<channel>[-<id>]' (real external channel: telegram, discord, etc.)

        Local scopes resolve to channel='tui' (we only do inbox fallback).
        External channels keep their channel slug for `openclaw message send`.

        Falls back to GenericAdapter's parser if structure is unexpected.
        """
        trajectory = jsonl_path.with_suffix(".trajectory.jsonl")
        try:
            if trajectory.exists():
                with trajectory.open("r", encoding="utf-8") as h:
                    for line in h:
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        session_key = obj.get("sessionKey", "")
                        if session_key:
                            return SessionMetadata(
                                session_id=str(obj.get("sessionId") or jsonl_path.stem),
                                language=GenericAdapter._detect_language(line),
                                channel=_classify_session_scope(session_key),
                                recipient=session_key,
                            )
        except OSError:
            pass
        return GenericAdapter().session_metadata(jsonl_path)

    @staticmethod
    def _discover_channels() -> tuple[str, ...]:
        """Best-effort: query `openclaw channels list --json`. Empty on failure."""
        try:
            result = _run_openclaw(["channels", "list", "--json"], timeout=10)
            if result.returncode != 0:
                return ()
            payload = json.loads(result.stdout or "{}")
            channels = payload.get("channels") or payload.get("accounts") or []
            return tuple(
                str(c.get("channel") or c.get("provider") or c.get("name", ""))
                for c in channels
                if isinstance(c, dict)
            )
        except (json.JSONDecodeError, OSError, subprocess.SubprocessError):
            return ()
