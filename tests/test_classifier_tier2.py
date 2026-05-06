"""Tests for Tier 2 host-inference classifier."""
import hashlib
import json
from pathlib import Path

import pytest

from agent_doctor.classifier.tier2 import Tier2Result, tier2_classify


class _FakeAdapter:
    """Minimal adapter implementing what tier2 calls."""
    def __init__(self, infer_response: str | None = None, can_infer: bool = True) -> None:
        self.infer_response = infer_response
        self.can_infer = can_infer
        self.call_count = 0

    def capabilities(self):
        from agent_doctor.adapters import HostCapabilities
        return HostCapabilities(
            host_name="fake",
            detected_at=Path("/"),
            can_infer_text=self.can_infer,
        )

    def infer_text(self, prompt, *, model=None):
        self.call_count += 1
        if self.infer_response is None:
            raise RuntimeError("no canned response")
        return self.infer_response


def test_tier2_skips_when_adapter_cannot_infer(tmp_path: Path) -> None:
    """Adapter with can_infer_text=False -> skipped=True."""
    adapter = _FakeAdapter(can_infer=False)
    result = tier2_classify("anything", adapter, cache_path=tmp_path / "cache.db")
    assert result.skipped is True
    assert result.severity == "none"


def test_tier2_parses_valid_json_response(tmp_path: Path) -> None:
    canned = json.dumps({
        "severity": "high",
        "signals": ["sarcasm", "trust_break"],
        "rationale": "user is sarcastic and giving up",
    })
    adapter = _FakeAdapter(infer_response=canned)
    result = tier2_classify("interesting choice", adapter, cache_path=tmp_path / "c.db")
    assert result.severity == "high"
    assert "sarcasm" in result.signals
    assert "trust_break" in result.signals
    assert result.rationale
    assert result.skipped is False
    assert result.cached is False


def test_tier2_caches_results(tmp_path: Path) -> None:
    """Second call with same text returns cached=True without invoking adapter."""
    canned = json.dumps({"severity": "low", "signals": [], "rationale": "x"})
    adapter = _FakeAdapter(infer_response=canned)
    cache = tmp_path / "c.db"

    r1 = tier2_classify("hi", adapter, cache_path=cache)
    assert r1.cached is False
    assert adapter.call_count == 1

    r2 = tier2_classify("hi", adapter, cache_path=cache)
    assert r2.cached is True
    assert adapter.call_count == 1  # no new call

    # Same severity reported
    assert r2.severity == r1.severity


def test_tier2_handles_malformed_json_with_retry(tmp_path: Path) -> None:
    """First response is invalid JSON; retry succeeds."""
    class _RetryAdapter(_FakeAdapter):
        def infer_text(self, prompt, *, model=None):
            self.call_count += 1
            if self.call_count == 1:
                return "definitely not JSON {{{"
            return json.dumps({"severity": "medium", "signals": ["x"], "rationale": "y"})
    adapter = _RetryAdapter()
    result = tier2_classify("hmm", adapter, cache_path=tmp_path / "c.db")
    assert result.severity == "medium"
    assert adapter.call_count == 2


def test_tier2_returns_skipped_after_double_parse_failure(tmp_path: Path) -> None:
    """Both attempts return invalid JSON -> skipped."""
    adapter = _FakeAdapter(infer_response="not json at all")
    result = tier2_classify("ok", adapter, cache_path=tmp_path / "c.db")
    assert result.skipped is True
    assert result.severity == "none"


def test_tier2_respects_daily_call_cap(tmp_path: Path) -> None:
    """When cap is exhausted, returns skipped=True."""
    adapter = _FakeAdapter(infer_response=json.dumps({"severity": "low", "signals": [], "rationale": "x"}))
    cache = tmp_path / "c.db"
    # Set cap to 2 — first 2 calls succeed, 3rd hits cap
    r1 = tier2_classify("a", adapter, cache_path=cache, max_calls_per_day=2)
    r2 = tier2_classify("b", adapter, cache_path=cache, max_calls_per_day=2)
    r3 = tier2_classify("c", adapter, cache_path=cache, max_calls_per_day=2)
    assert r1.skipped is False
    assert r2.skipped is False
    assert r3.skipped is True
    assert "cap" in r3.rationale.lower() or r3.rationale == ""


def test_tier2_handles_adapter_runtime_error(tmp_path: Path) -> None:
    """If adapter.infer_text raises, return skipped=True (don't blow up)."""
    adapter = _FakeAdapter(infer_response=None)  # raises RuntimeError
    result = tier2_classify("x", adapter, cache_path=tmp_path / "c.db")
    assert result.skipped is True


def test_tier2_passes_model_override(tmp_path: Path) -> None:
    """If model arg is given, it's passed to adapter.infer_text."""
    captured = {}

    class _Capturing(_FakeAdapter):
        def infer_text(self, prompt, *, model=None):
            captured["model"] = model
            return json.dumps({"severity": "none", "signals": [], "rationale": ""})

    adapter = _Capturing()
    tier2_classify("x", adapter, cache_path=tmp_path / "c.db", model="claude-haiku")
    assert captured["model"] == "claude-haiku"
