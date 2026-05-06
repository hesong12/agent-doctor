"""Contract tests for the adapter base types.

The Protocol is structural; we don't test it directly. We test the
dataclasses are frozen (immutability matters when these flow through
multiple subprocess hops) and that defaults behave.
"""
from pathlib import Path

import pytest

from agent_doctor.adapters import (
    HostAdapter,
    HostCapabilities,
    MessageBody,
    MessageKind,
    Reaction,
    SessionMetadata,
    Target,
)


def test_target_is_frozen() -> None:
    target = Target(host="openclaw", channel="telegram", recipient="@me")
    with pytest.raises((AttributeError, TypeError)):
        target.host = "hermes"  # type: ignore[misc]


def test_target_supports_tui_kind_with_inbox_path(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    target = Target(host="openclaw", channel="tui", recipient="local", inbox_path=inbox)
    assert target.kind() == "tui"
    assert target.inbox_path == inbox


def test_message_kind_values() -> None:
    assert MessageKind.intervene.value == "intervene"
    assert MessageKind.propose.value == "propose"
    assert MessageKind.digest.value == "digest"
    assert MessageKind.applied.value == "applied"
    assert MessageKind.undone.value == "undone"


def test_message_body_must_have_header_and_body() -> None:
    body = MessageBody(header="🩺 Agent Doctor — intervene", body="evidence …", footer=None)
    assert "🩺" in body.header
    assert body.body
    assert body.footer is None


def test_message_body_rejects_empty_header_or_body() -> None:
    with pytest.raises(ValueError):
        MessageBody(header="", body="anything")
    with pytest.raises(ValueError):
        MessageBody(header="anything", body="")


def test_message_body_render_returns_full_text() -> None:
    body = MessageBody(header="H", body="B", footer="F")
    assert body.render() == "H\n\nB\n\nF"


def test_message_body_render_without_footer() -> None:
    body = MessageBody(header="H", body="B")
    assert body.render() == "H\n\nB"


def test_reaction_dataclass() -> None:
    r = Reaction(message_id="m1", emoji="✅", user_id="u1", at=1.0)
    assert r.emoji == "✅"


def test_session_metadata_defaults() -> None:
    meta = SessionMetadata(session_id="s", language="zh", channel="telegram", recipient="@me")
    assert meta.language == "zh"


def test_host_capabilities_coerces_iterables_to_tuple() -> None:
    """Adapter authors who pass list-shaped values for available_models /
    available_channels should not break the frozen-dataclass invariants
    expected downstream (e.g., `caps.available_models + ("foo",)`)."""
    caps = HostCapabilities(
        host_name="x",
        detected_at=Path("/"),
        available_models=["a", "b"],  # type: ignore[arg-type]
        available_channels=["telegram"],  # type: ignore[arg-type]
    )
    assert isinstance(caps.available_models, tuple)
    assert isinstance(caps.available_channels, tuple)
    assert caps.available_models == ("a", "b")
    assert caps.available_channels == ("telegram",)


def test_host_capabilities_defaults_are_conservative() -> None:
    """A new adapter that doesn't override anything should be maximally
    degraded, so missing implementations never silently appear capable."""
    caps = HostCapabilities(host_name="x", detected_at=Path("/"))
    assert caps.can_send_message is False
    assert caps.can_react is False
    assert caps.can_list_reactions is False
    assert caps.can_edit_message is False
    assert caps.can_inject_system_event is False
    assert caps.can_infer_text is False
    assert caps.can_infer_embedding is False
    assert caps.default_inference_model is None
    assert caps.available_models == ()
    assert caps.available_channels == ()
    assert caps.skill_dir is None
    assert caps.memory_writable is None
    assert caps.identity_writable is None
    assert caps.sop_writable is None


def test_host_adapter_is_runtime_checkable() -> None:
    """isinstance check should work via Protocol so consumers can branch."""
    class _Stub:
        @classmethod
        def detect(cls): return None
        def capabilities(self): return HostCapabilities(host_name="stub", detected_at=Path("/"))
        def send_message(self, target, body, kind): return ""
        def edit_message(self, target, message_id, body): pass
        def add_reaction(self, target, message_id, emoji): pass
        def list_reactions(self, target, message_id): return []
        def inject_system_event(self, text, *, mode="now"): pass
        def infer_text(self, prompt, *, model=None): return ""
        def infer_embedding(self, text, *, model=None): return []
        def session_metadata(self, jsonl_path): return SessionMetadata(session_id="", language="en", channel="generic", recipient="local")

    assert isinstance(_Stub(), HostAdapter)
