"""Tests for GenericAdapter — the always-available fallback.

Generic supports inbox-file delivery and OS notifications. It does not
support sending into chat channels, reactions, system events, or
inference. Capabilities reflect this honestly so downstream code
branches correctly.
"""
from pathlib import Path

import pytest

from agent_doctor.adapters import (
    GenericAdapter,
    HostAdapter,
    HostCapabilities,
    MessageBody,
    MessageKind,
    Target,
)


def test_generic_adapter_is_a_host_adapter() -> None:
    assert isinstance(GenericAdapter(), HostAdapter)


def test_generic_detect_always_returns_an_instance(tmp_path: Path) -> None:
    """Generic is the always-available fallback; detect never returns None."""
    instance = GenericAdapter.detect()
    assert isinstance(instance, GenericAdapter)


def test_generic_capabilities_are_minimal() -> None:
    caps = GenericAdapter().capabilities()
    assert caps.host_name == "generic"
    assert caps.can_send_message is False
    assert caps.can_react is False
    assert caps.can_list_reactions is False
    assert caps.can_inject_system_event is False
    assert caps.can_infer_text is False
    assert caps.can_infer_embedding is False
    assert caps.available_channels == ()


def test_generic_send_message_writes_inbox_file(tmp_path: Path) -> None:
    """Generic's send_message writes to the target's inbox_path."""
    inbox = tmp_path / "inbox.md"
    target = Target(host="generic", channel="inbox", recipient="", inbox_path=inbox)
    body = MessageBody(header="🩺 Agent Doctor — intervene", body="user is angry", footer=None)

    message_id = GenericAdapter().send_message(target, body, MessageKind.intervene)

    assert inbox.exists()
    text = inbox.read_text(encoding="utf-8")
    assert "🩺" in text
    assert "user is angry" in text
    assert message_id  # opaque but non-empty


def test_generic_send_message_without_inbox_path_raises() -> None:
    """Capability flag says we don't send messages — explicit error if called anyway."""
    target = Target(host="generic", channel="inbox", recipient="")  # no inbox_path
    body = MessageBody(header="h", body="b")

    with pytest.raises(NotImplementedError):
        GenericAdapter().send_message(target, body, MessageKind.intervene)


def test_generic_list_reactions_returns_empty() -> None:
    target = Target(host="generic", channel="inbox", recipient="")
    assert GenericAdapter().list_reactions(target, "any") == []


def test_generic_infer_text_raises() -> None:
    with pytest.raises(NotImplementedError):
        GenericAdapter().infer_text("anything")


def test_generic_session_metadata_parses_basic_jsonl(tmp_path: Path) -> None:
    """Generic adapter's metadata parser reads only the basics it can guess
    from a JSONL — session_id from filename, language defaulted to 'en'."""
    jsonl = tmp_path / "abc-123.jsonl"
    jsonl.write_text(
        '{"session_id": "abc-123", "role": "user", "content": "hello"}\n',
        encoding="utf-8",
    )

    meta = GenericAdapter().session_metadata(jsonl)

    assert meta.session_id  # parsed from JSONL or filename
    assert meta.language in ("en", "zh", "auto")


# --- Contract conformance ----------------------------------------------------

from agent_doctor.adapters.testing import AdapterContractTest


class TestGenericAdapterContract(AdapterContractTest):
    ADAPTER = GenericAdapter
