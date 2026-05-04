"""JSONL ingestion and normalization."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .schema import Message, Role

DEFAULT_HERMES_PATH = Path.home() / ".hermes" / "sessions"
DEFAULT_OPENCLAW_PATH = Path.home() / ".openclaw" / "agents" / "main" / "sessions"


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


def ingest_path(path: Path) -> list[Message]:
    messages: list[Message] = []
    for jsonl_path in collect_jsonl_paths(path):
        messages.extend(ingest_file(jsonl_path))
    return messages


def ingest_file(path: Path) -> list[Message]:
    default_session = path.stem
    messages: list[Message] = []

    with path.expanduser().open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise IngestError(f"{path}:{line_number} is not valid JSON: {exc}") from exc
            if not isinstance(event, dict):
                event = {"content": event, "type": "metadata"}
            message = normalize_event(event, path, line_number, default_session)
            if message.content.strip():
                messages.append(message)
    return messages


def normalize_event(
    event: dict[str, Any], path: Path, line_number: int, default_session: str
) -> Message:
    source_format = detect_source_format(event, path)
    session_id = _first_text(
        event,
        [
            "session_id",
            "sessionId",
            "session",
            "conversation_id",
            "conversationId",
            "thread_id",
            "threadId",
            "run_id",
            "runId",
        ],
    )
    payload = event.get("payload")
    if not session_id and isinstance(payload, dict):
        session_id = _first_text(payload, ["session_id", "sessionId", "session"])
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
    filename = path.name.casefold()
    if "hermes" in filename:
        return "hermes"
    if "openclaw" in filename or "open-claw" in filename:
        return "openclaw"

    marker = " ".join(str(event.get(key, "")) for key in ("app", "client", "source", "agent"))
    marker = marker.casefold()
    if "hermes" in marker:
        return "hermes"
    if "openclaw" in marker or "open-claw" in marker:
        return "openclaw"
    if "payload" in event and ("event" in event or "actor" in event):
        return "openclaw"
    return "generic"


def normalize_role(event: dict[str, Any]) -> Role:
    payload = event.get("payload")
    candidates: list[Any] = [
        event.get("role"),
        event.get("actor"),
        event.get("speaker"),
        event.get("source"),
        event.get("sender"),
        event.get("type"),
        event.get("event"),
        event.get("kind"),
    ]
    if isinstance(payload, dict):
        candidates.extend(
            [
                payload.get("role"),
                payload.get("actor"),
                payload.get("speaker"),
                payload.get("source"),
                payload.get("sender"),
                payload.get("type"),
                payload.get("event"),
            ]
        )

    for candidate in candidates:
        role = _role_from_value(candidate)
        if role:
            return role

    if any(key in event for key in ("tool_call_id", "tool_name", "command", "stderr")):
        return "tool"
    return "system/metadata"


def extract_content(event: dict[str, Any]) -> str:
    for container in _containers(event):
        value = _first_present(
            container,
            [
                "content",
                "text",
                "message",
                "body",
                "output",
                "result",
                "stderr",
                "stdout",
                "error",
                "summary",
            ],
        )
        if value is not None:
            return _stringify_content(value)
    return _stringify_content(event)


def _containers(event: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield event
    for key in ("payload", "data", "message", "entry"):
        value = event.get(key)
        if isinstance(value, dict):
            yield value


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
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [_stringify_content(item) for item in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("text", "content", "message", "body", "output", "error", "result"):
            if key in value and value[key] is not None:
                return _stringify_content(value[key])
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip()
