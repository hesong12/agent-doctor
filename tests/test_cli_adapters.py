"""Tests for new CLI commands `agent-doctor adapters list / test`."""
import json
import os
import subprocess
import sys
from pathlib import Path


def test_adapters_list_returns_json(tmp_path: Path) -> None:
    """`agent-doctor adapters list --json` prints capability matrix."""
    env = {
        **os.environ,
        "HOME": str(tmp_path),  # no ~/.openclaw, no ~/.hermes
    }
    result = subprocess.run(
        [sys.executable, "-m", "agent_doctor.cli", "adapters", "list", "--json"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    names = [item["host_name"] for item in payload]
    assert "generic" in names  # always present


def test_adapters_list_text_output_works(tmp_path: Path) -> None:
    """Default text output (no --json) is human-readable."""
    env = {**os.environ, "HOME": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-m", "agent_doctor.cli", "adapters", "list"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "generic" in result.stdout
    assert "can_send_message" in result.stdout


def test_adapters_test_unknown_host_returns_2(tmp_path: Path) -> None:
    env = {**os.environ, "HOME": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-m", "agent_doctor.cli", "adapters", "test", "nope"],
        capture_output=True,
        text=True,
        env=env,
    )
    # argparse should error out with rc=2 because "nope" isn't in choices
    assert result.returncode == 2


def test_adapters_test_undetected_host_returns_3(tmp_path: Path) -> None:
    """When the host's home dir doesn't exist, exit code 3."""
    env = {**os.environ, "HOME": str(tmp_path)}  # no ~/.hermes here
    result = subprocess.run(
        [sys.executable, "-m", "agent_doctor.cli", "adapters", "test", "hermes"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 3
    assert "hermes" in result.stderr.lower() or "hermes" in result.stdout.lower()


def test_adapters_test_generic_returns_0(tmp_path: Path) -> None:
    """Generic is always detected → exit 0."""
    env = {**os.environ, "HOME": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-m", "agent_doctor.cli", "adapters", "test", "generic"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0
    assert "generic" in result.stdout.lower()
