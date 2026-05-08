"""Tests for measurer."""
import json
import time
from pathlib import Path

import pytest

from agent_doctor.measurer import Measurement, append_measurement, measure_patch


class _FakeAdapter:
    def __init__(self, response: str | None = None, can_infer: bool = True):
        self.response = response
        self.can_infer = can_infer
        self.calls = 0

    def capabilities(self):
        from agent_doctor.adapters import HostCapabilities
        return HostCapabilities(host_name="fake", detected_at=Path("/"), can_infer_text=self.can_infer)

    def infer_text(self, prompt, *, model=None):
        self.calls += 1
        if self.response is None:
            raise RuntimeError("fail")
        return self.response


def _entry(patch_id: str = "p-1", target_kind: str = "memory") -> dict:
    return {
        "id": patch_id,
        "session_id": "s-1",
        "target_kind": target_kind,
        "target_file": "/tmp/MEMORY.md",
        "backup_path": "/tmp/backup.bak",
        "applied_at": time.time(),
        "patch_body": "- User dislikes verbose output.",  # Real patch body needed
    }


def test_measure_returns_score_from_judge_response() -> None:
    adapter = _FakeAdapter(response=json.dumps({"score": 0.9, "rationale": "addresses verbose-output complaint"}))
    entry = _entry()
    entry["patch_body"] = "- User dislikes verbose output."

    m = measure_patch(
        entry,
        evidence_text="why is your output so noisy?",
        adapter=adapter,
    )
    assert m.score == 0.9
    assert "verbose" in m.rationale.lower() or "noisy" in m.rationale.lower() or m.rationale
    assert m.patch_id == "p-1"
    assert m.session_id == "s-1"


def test_measure_returns_neutral_when_adapter_cannot_infer() -> None:
    adapter = _FakeAdapter(can_infer=False)
    entry = _entry()
    entry["patch_body"] = "x"

    m = measure_patch(
        entry,
        evidence_text="any",
        adapter=adapter,
    )
    assert m.score == 0.5
    assert "unmeasured" in m.rationale.lower() or "no infer" in m.rationale.lower()


def test_measure_handles_adapter_runtime_error() -> None:
    """Adapter raises → score=0.5, neutral."""
    adapter = _FakeAdapter(response=None)  # will raise
    entry = _entry()
    entry["patch_body"] = "x"

    m = measure_patch(entry, evidence_text="y", adapter=adapter)
    assert m.score == 0.5


def test_measure_handles_malformed_judge_response() -> None:
    """Judge returns non-JSON → score=0.5."""
    adapter = _FakeAdapter(response="not json")
    entry = _entry()
    entry["patch_body"] = "x"

    m = measure_patch(entry, evidence_text="y", adapter=adapter)
    assert m.score == 0.5


def test_measure_clamps_score_to_unit_interval() -> None:
    """Judge returns score=1.5 (out of bounds) → clamped to 1.0."""
    adapter = _FakeAdapter(response=json.dumps({"score": 1.5, "rationale": "x"}))
    entry = _entry()
    entry["patch_body"] = "x"

    m = measure_patch(entry, evidence_text="y", adapter=adapter)
    assert m.score == 1.0


def test_append_measurement_writes_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "measurements.jsonl"
    m = Measurement(
        patch_id="p-1",
        session_id="s-1",
        target_kind="memory",
        score=0.9,
        judged_by_model="claude-haiku",
        rationale="ok",
        measured_at=time.time(),
    )
    append_measurement(path, m)

    line = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert line["patch_id"] == "p-1"
    assert line["score"] == 0.9
    import stat
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
