"""Tests for calibrate CLI scaffolds."""
import os
import subprocess
import sys
from pathlib import Path


def _run(args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "agent_doctor.cli"] + args,
        capture_output=True, text=True, env=env,
    )


def test_calibrate_status_default_disabled(tmp_path: Path) -> None:
    env = {**os.environ, "HOME": str(tmp_path)}
    r = _run(["calibrate", "status"], env=env)
    assert r.returncode == 0
    assert "False" in r.stdout or "disabled" in r.stdout.lower()


def test_calibrate_enable_creates_flag(tmp_path: Path) -> None:
    env = {**os.environ, "HOME": str(tmp_path)}
    r = _run(["calibrate", "enable"], env=env)
    assert r.returncode == 0
    assert (tmp_path / ".agent-doctor" / "calibrate-enabled").exists()


def test_calibrate_disable_removes_flag(tmp_path: Path) -> None:
    env = {**os.environ, "HOME": str(tmp_path)}
    _run(["calibrate", "enable"], env=env)
    r = _run(["calibrate", "disable"], env=env)
    assert r.returncode == 0
    assert not (tmp_path / ".agent-doctor" / "calibrate-enabled").exists()


def test_calibrate_review_no_suggestions(tmp_path: Path) -> None:
    env = {**os.environ, "HOME": str(tmp_path)}
    r = _run(["calibrate", "review"], env=env)
    assert r.returncode == 0
    assert "no calibration suggestions" in r.stdout.lower()
