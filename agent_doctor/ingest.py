"""JSONL ingestion and normalization.

The ingestion layer is the resilience boundary for Agent Doctor. Real
transcript stores contain malformed lines (logger crashes mid-write, partial
flushes, occasional binary content) and very large tool outputs (full build
logs, multi-megabyte stack traces). The MVP refused to ingest a directory if
any line was bad and held entire stdout payloads in memory verbatim — both
broke real-world use.

This module now:

- Skips malformed lines by default, surfacing a ``parse_errors`` count via
  :func:`ingest_path_with_errors`. ``--strict`` callers can opt back into the
  hard-fail behavior.
- Caps individual content payloads at ``MAX_CONTENT_CHARS`` so a single huge
  tool message cannot dominate downstream detection or memory use.

The normalization rules themselves (container walking, role inference, source
detection) are unchanged.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

from .schema import Message, Role

def host_home() -> Path:
    """Return the real host home when running inside an agent sandbox.

    OpenClaw/Codex-style sandboxes often set ``HOME`` to a nested directory
    below the real user's ``~/.openclaw`` tree. Agent Doctor's default
    platform adapters should inspect the host transcripts, not an empty
    sandbox home, so we peel back to the directory before ``.openclaw`` /
    ``.hermes`` when that shape is visible. Users can override explicitly
    with ``AGENT_DOCTOR_HOST_HOME``.
    """

    override = os.environ.get("AGENT_DOCTOR_HOST_HOME")
    if override:
        return Path(override).expanduser()
    home = Path.home().expanduser()
    parts = home.parts
    for marker in (".openclaw", ".hermes"):
        if marker in parts:
            index = parts.index(marker)
            if index > 0:
                return Path(*parts[:index])
    return home


DEFAULT_HERMES_PATH = host_home() / ".hermes" / "sessions"
DEFAULT_OPENCLAW_PATH = host_home() / ".openclaw" / "agents" / "main" / "sessions"
CONTAINER_KEYS = ("message", "data", "entry", "payload")
MAX_CONTENT_CHARS = 8000
CONTENT_KEYS = (
    "content",
    "text",
    "body",
    "output",
    "result",
    "stderr",
    "stdout",
    "error",
    "summary",
)
SESSION_KEYS = (
    "session_id",
    "sessionId",
    "session",
    "conversation_id",
    "conversationId",
    "thread_id",
    "threadId",
    "run_id",
    "runId",
)
UUID_STEM = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class IngestError(ValueError):
    """Raised when transcript input cannot be read as JSONL."""


def collect_jsonl_paths(path: Path) -> list[Path]:
    """Return a stable list of JSONL files from a file or directory."""

    expanded = path.expanduser()
    if not expanded.exists():
        raise IngestError(f"Input path does not exist: {expanded}")
    if expanded.is_file():
        return [expanded]
    return sorted(item for item in expanded.rglob("*.jsonl") if item.is_file())


def ingest_path(path: Path, *, strict: bool = False) -> list[Message]:
    messages, _ = ingest_path_with_errors(path, strict=strict)
    return messages


def ingest_path_with_errors(
    path: Path, *, strict: bool = False
) -> tuple[list[Message], int]:
    messages: list[Message] = []
    parse_errors = 0
    for jsonl_path in collect_jsonl_paths(path):
        file_messages, file_errors = ingest_file_with_errors(jsonl_path, strict=strict)
        messages.extend(file_messages)
        parse_errors += file_errors
    return messages, parse_errors


def ingest_file(path: Path, *, strict: bool = False) -> list[Message]:
    messages, _ = ingest_file_with_errors(path, strict=strict)
    return messages


def ingest_file_with_errors(
    path: Path, *, strict: bool = False
) -> tuple[list[Message], int]:
    default_session = path.stem
    messages: list[Message] = []
    parse_errors = 0

    with path.expanduser().open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError as exc:
                if strict:
                    raise IngestError(
                        f"{path}:{line_number} is not valid JSON: {exc}"
                    ) from exc
                parse_errors += 1
                continue
            if not isinstance(event, dict):
                event = {"content": event, "type": "metadata"}
            message = normalize_event(event, path, line_number, default_session)
            if message.content.strip():
                messages.append(message)
    return messages, parse_errors


def normalize_event(
    event: dict[str, Any], path: Path, line_number: int, default_session: str
) -> Message:
    source_format = detect_source_format(event, path)
    session_id = None
    for container in _containers(event):
        session_id = _first_text(container, list(SESSION_KEYS))
        if session_id:
            break
    session_id = session_id or default_session

    raw_type = _first_text(event, ["type", "event", "kind", "category"]) or ""
    role = normalize_role(event)
    content = extract_content(event)

    return Message(
        file=str(path),
        line=line_number,
        session_id=session_id,
        role=role,
        content=content,
        source_format=source_format,
        raw_type=raw_type,
    )


def detect_source_format(event: dict[str, Any], path: Path) -> str:
    path_text = str(path).casefold()
    if "hermes" in path_text:
        return "hermes"
    if "openclaw" in path_text or "open-claw" in path_text:
        return "openclaw"

    marker = " ".join(
        str(container.get(key, ""))
        for container in _containers(event)
        for key in ("app", "client", "source", "agent")
    )
    marker = marker.casefold()
    if "hermes" in marker:
        return "hermes"
    if "openclaw" in marker or "open-claw" in marker:
        return "openclaw"
    if UUID_STEM.fullmatch(path.stem) and _has_nested_message_role_and_content(event):
        return "openclaw"
    if "payload" in event and ("event" in event or "actor" in event):
        return "openclaw"
    return "generic"


def normalize_role(event: dict[str, Any]) -> Role:
    for container in _containers(event):
        candidates: list[Any] = [
            container.get("role"),
            container.get("actor"),
            container.get("speaker"),
            container.get("source"),
            container.get("sender"),
            container.get("type"),
            container.get("event"),
            container.get("kind"),
        ]
        for candidate in candidates:
            role = _role_from_value(candidate)
            if role:
                return role

    for container in _containers(event):
        if any(
            key in container
            for key in ("tool_call_id", "tool_name", "command", "stderr", "stdout")
        ):
            return "tool"
    return "system/metadata"


def extract_content(event: dict[str, Any]) -> str:
    for container in _containers(event):
        value = _first_present(container, list(CONTENT_KEYS))
        if value is not None:
            return _stringify_content(value)

    for container in _containers(event):
        value = _first_present(container, list(CONTAINER_KEYS))
        if value is not None:
            return _stringify_content(value)
    return _stringify_content(event)


def _containers(event: dict[str, Any]) -> Iterable[dict[str, Any]]:
    pending = [event]
    seen: set[int] = set()
    while pending:
        container = pending.pop(0)
        container_id = id(container)
        if container_id in seen:
            continue
        seen.add(container_id)
        yield container
        for key in CONTAINER_KEYS:
            value = container.get(key)
            if isinstance(value, dict):
                pending.append(value)


def _has_nested_message_role_and_content(event: dict[str, Any]) -> bool:
    for container in _containers(event):
        if container is event:
            continue
        has_role = _first_present(
            container,
            [
                "role",
                "actor",
                "speaker",
                "source",
                "sender",
                "type",
                "event",
                "kind",
            ],
        )
        has_content = _first_present(container, list(CONTENT_KEYS))
        if has_role is not None and has_content is not None:
            return True
    return False


def _role_from_value(value: Any) -> Role | None:
    if value is None:
        return None
    text = str(value).strip().casefold().replace("-", "_")
    if not text:
        return None
    if any(token in text for token in ("tool", "function", "command", "shell", "terminal")):
        return "tool"
    if any(token in text for token in ("assistant", "agent", "ai", "model")):
        return "assistant"
    if any(token in text for token in ("user", "human", "customer", "operator")):
        return "user"
    if any(token in text for token in ("system", "metadata", "meta", "debug")):
        return "system/metadata"
    return None


def _first_text(container: dict[str, Any], keys: list[str]) -> str | None:
    value = _first_present(container, keys)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_present(container: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        if key in container and container[key] is not None:
            return container[key]
    return None


def _stringify_content(value: Any) -> str:
    text = _stringify_raw(value)
    if len(text) > MAX_CONTENT_CHARS:
        text = text[:MAX_CONTENT_CHARS].rstrip() + "\n... [truncated]"
    return text


_NOISY_KEYS_DROPPED_FROM_JSON = frozenset(
    {"thoughtSignature", "thought_signature", "signature", "logprobs", "raw"}
)


def _stringify_raw(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [_stringify_raw(item) for item in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        # OpenClaw / Anthropic / OpenAI-style typed content parts. We unwrap to
        # the most informative inner field so report quotes read like real
        # transcript excerpts instead of stringified JSON.
        part_type = str(value.get("type", "")).strip().casefold()
        if part_type in {"text", "input_text", "output_text"} and isinstance(value.get("text"), str):
            return value["text"].strip()
        if part_type == "thinking" and isinstance(value.get("thinking"), str):
            return f"[thinking] {value['thinking'].strip()}"
        if part_type in {"toolcall", "tool_use", "tool_call", "function_call"}:
            name = value.get("name") or value.get("tool") or "tool"
            args = value.get("arguments") or value.get("input") or {}
            return f"[tool_call: {name}({_compact_args(args)})]"
        if part_type in {"toolresult", "tool_result", "function_result"}:
            inner = value.get("content") or value.get("output") or value.get("result")
            # If none of the expected inner-content fields are present, fall
            # through to the generic CONTENT_KEYS / json.dumps path below
            # rather than recursing on `value` itself (would cause infinite
            # recursion for a stub `{"type": "tool_result"}` shape).
            if inner is not None:
                return _stringify_raw(inner)
        for key in CONTENT_KEYS + CONTAINER_KEYS:
            if key in value and value[key] is not None:
                return _stringify_raw(value[key])
        cleaned = {k: v for k, v in value.items() if k not in _NOISY_KEYS_DROPPED_FROM_JSON}
        return json.dumps(cleaned, ensure_ascii=False, sort_keys=True)
    return str(value).strip()


def _compact_args(args: Any) -> str:
    if isinstance(args, str):
        return args.strip()[:200]
    if isinstance(args, dict):
        # Filter out noisy keys *first*, then take the first 6 informative
        # ones. If we sliced before filtering, an args dict whose first six
        # keys are all noise (`thoughtSignature`, `signature`, …) would
        # render as empty even when the real signal was at index 7+.
        informative = [
            (k, v) for k, v in args.items() if k not in _NOISY_KEYS_DROPPED_FROM_JSON
        ][:6]
        parts = []
        for key, val in informative:
            text = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
            text = text.strip()
            if len(text) > 80:
                text = text[:77] + "..."
            parts.append(f"{key}={text}")
        return ", ".join(parts)
    return str(args)[:200]
