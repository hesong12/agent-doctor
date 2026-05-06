"""AdapterContractTest: a reusable test class.

Any adapter implementation should subclass this and set ADAPTER to its
adapter type. The class verifies the adapter is structurally a
HostAdapter, that its capabilities() is well-formed, and that all
capability-gated methods either work or raise NotImplementedError
consistent with the declared capabilities.

Usage:

    from agent_doctor.adapters import GenericAdapter
    from agent_doctor.adapters.testing import AdapterContractTest

    class TestGenericContract(AdapterContractTest):
        ADAPTER = GenericAdapter
"""
from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from .base import (
    HostAdapter,
    HostCapabilities,
    MessageBody,
    MessageKind,
    Target,
)


class AdapterContractTest:
    """Subclass and set ADAPTER to validate an adapter against the contract."""

    ADAPTER: ClassVar[type[HostAdapter]]  # must be set by subclass

    @pytest.fixture()
    def adapter(self) -> HostAdapter:
        instance = self.ADAPTER.detect()
        if instance is None:
            pytest.skip(f"{self.ADAPTER.__name__}.detect() returned None on this machine.")
        return instance

    def test_adapter_is_host_adapter(self, adapter: HostAdapter) -> None:
        assert isinstance(adapter, HostAdapter)

    def test_capabilities_is_well_formed(self, adapter: HostAdapter) -> None:
        caps = adapter.capabilities()
        assert isinstance(caps, HostCapabilities)
        assert caps.host_name
        assert isinstance(caps.detected_at, Path)
        # Booleans must be bools, not None or strings
        for flag_name in (
            "can_send_message",
            "can_edit_message",
            "can_react",
            "can_list_reactions",
            "can_inject_system_event",
            "can_infer_text",
            "can_infer_embedding",
        ):
            assert isinstance(getattr(caps, flag_name), bool)
        # Tuples not lists — frozen-dataclass invariants downstream
        # (e.g., `caps.available_models + ("foo",)`) require this.
        assert isinstance(caps.available_models, tuple)
        assert isinstance(caps.available_channels, tuple)

    def test_send_message_respects_capability(self, adapter: HostAdapter, tmp_path: Path) -> None:
        caps = adapter.capabilities()
        target = Target(
            host=caps.host_name,
            channel="inbox",
            recipient="",
            inbox_path=tmp_path / "msg.md",
        )
        body = MessageBody(header="🩺 contract test", body="hello", footer=None)
        if caps.can_send_message or target.inbox_path is not None:
            # Either flag is True (real send) or generic-fallback path
            # (inbox file write) — both should succeed.
            try:
                msg_id = adapter.send_message(target, body, MessageKind.intervene)
                assert msg_id  # adapters return some opaque, non-empty id
            except NotImplementedError:
                # Adapter explicitly opts out — must declare flag False
                assert caps.can_send_message is False
        else:
            with pytest.raises(NotImplementedError):
                adapter.send_message(target, body, MessageKind.intervene)

    def test_list_reactions_respects_capability(self, adapter: HostAdapter, tmp_path: Path) -> None:
        caps = adapter.capabilities()
        target = Target(
            host=caps.host_name,
            channel="inbox",
            recipient="",
            inbox_path=tmp_path / "msg.md",
        )
        result = adapter.list_reactions(target, "fake-msg-id")
        # Whether or not flag is True, the method must return a list (possibly empty).
        assert isinstance(result, list)

    def test_infer_text_respects_capability(self, adapter: HostAdapter) -> None:
        caps = adapter.capabilities()
        if caps.can_infer_text:
            # We don't actually run inference in the contract test —
            # downstream tests can. Just verify the flag is honored.
            assert callable(adapter.infer_text)
        else:
            with pytest.raises(NotImplementedError):
                adapter.infer_text("ping")
