"""Tests for install + bootstrap (easy-install path)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_doctor.bootstrap import (
    bootstrap,
    detect_hosts,
    invalidate_host_caches,
    mcp_config_snippet,
    render_bootstrap_summary,
)
from agent_doctor.install import VALID_TARGETS, install_skill


def test_install_skill_supports_all_documented_targets(tmp_path: Path) -> None:
    written: list[Path] = []
    for target in sorted(VALID_TARGETS):
        out = tmp_path / target
        path = install_skill(target, out)
        written.append(path)
        assert path.exists()
        text = path.read_text(encoding="utf-8")
        assert "Agent Doctor" in text


def test_claude_code_skill_has_required_frontmatter(tmp_path: Path) -> None:
    path = install_skill("claude-code", tmp_path)
    text = path.read_text(encoding="utf-8")

    assert text.startswith("---\n")
    assert "name: agent-doctor" in text
    assert "description:" in text
    assert text.split("---", 2)[1].strip().splitlines()[0].startswith("name: agent-doctor")
    assert path.parent.name == "agent-doctor"
    assert path.name == "SKILL.md"


def test_install_skill_normalizes_aliases(tmp_path: Path) -> None:
    a = install_skill("claude", tmp_path / "a")
    b = install_skill("Claude_Code", tmp_path / "b")
    assert a.name == "SKILL.md"
    assert b.name == "SKILL.md"


def test_install_skill_rejects_unknown_target(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        install_skill("nonsense", tmp_path)


def test_detect_hosts_only_marks_existing_dirs(tmp_path: Path) -> None:
    (tmp_path / ".hermes").mkdir()
    hosts = detect_hosts(home=tmp_path)
    by_name = {host.name: host for host in hosts}
    assert by_name["Hermes"].detected is True
    assert by_name["OpenClaw"].detected is False


def test_bootstrap_skips_undetected_hosts_without_force(tmp_path: Path) -> None:
    (tmp_path / ".claude" / "skills").mkdir(parents=True)

    result = bootstrap(home=tmp_path)

    by_name = {host.name: host for host in result.hosts}
    assert by_name["Hermes"].written_path is None
    assert by_name["OpenClaw"].written_path is None
    assert by_name["Claude Code"].written_path is not None
    assert by_name["Claude Code"].written_path.exists()


def test_bootstrap_dry_run_writes_nothing(tmp_path: Path) -> None:
    (tmp_path / ".hermes").mkdir()

    result = bootstrap(home=tmp_path, dry_run=True)

    hermes = next(host for host in result.hosts if host.name == "Hermes")
    assert hermes.written_path is not None  # records the *would-be* path
    assert hermes.written_path.exists() is False


def test_bootstrap_force_installs_into_missing_host(tmp_path: Path) -> None:
    result = bootstrap(home=tmp_path, force=True, extra_targets=["hermes"])

    forced = next(
        host for host in result.hosts if host.target == "hermes" and host.written_path is not None
    )
    assert forced.written_path.exists()


def test_bootstrap_extra_targets_create_directory(tmp_path: Path) -> None:
    result = bootstrap(home=tmp_path, extra_targets=["claude-code", "generic"], force=True)

    paths = {host.target: host.written_path for host in result.hosts if host.written_path}
    assert paths["claude-code"].exists()
    assert paths["generic"].exists()


def test_bootstrap_summary_mentions_mcp_snippet() -> None:
    snippet = mcp_config_snippet()
    parsed = json.loads(snippet)
    assert "mcpServers" in parsed
    assert "agent-doctor" in parsed["mcpServers"]
    assert parsed["mcpServers"]["agent-doctor"]["command"] == "agent-doctor"
    assert parsed["mcpServers"]["agent-doctor"]["args"] == ["mcp", "serve"]


def test_invalidate_cache_removes_hermes_snapshot(tmp_path: Path) -> None:
    """`bootstrap --invalidate-cache` removes Hermes's prebuilt skill prompt.

    Without this, Hermes won't pick up the new SKILL.md until the next
    process restart. With it, Hermes rebuilds on next start automatically.
    """

    hermes_root = tmp_path / ".hermes"
    hermes_root.mkdir()
    snapshot = hermes_root / ".skills_prompt_snapshot.json"
    snapshot.write_text("{stale}", encoding="utf-8")

    result = bootstrap(home=tmp_path, invalidate_cache=True)

    assert not snapshot.exists(), "stale skills_prompt_snapshot.json should be removed"
    hermes_invalidations = [inv for inv in result.invalidations if inv.host == "Hermes"]
    assert hermes_invalidations and hermes_invalidations[0].succeeded


def test_invalidate_cache_handles_missing_snapshot(tmp_path: Path) -> None:
    """Missing snapshot is fine — we record success with a 'nothing to do' note."""

    (tmp_path / ".hermes").mkdir()
    result = bootstrap(home=tmp_path, invalidate_cache=True)
    hermes_invalidations = [inv for inv in result.invalidations if inv.host == "Hermes"]
    assert hermes_invalidations and hermes_invalidations[0].succeeded
    assert "nothing to invalidate" in hermes_invalidations[0].detail


def test_invalidate_host_caches_runs_independently(tmp_path: Path) -> None:
    """The helper is callable on its own (used by tooling that drives bootstrap)."""

    (tmp_path / ".hermes").mkdir()
    snapshot = tmp_path / ".hermes" / ".skills_prompt_snapshot.json"
    snapshot.write_text("{stale}", encoding="utf-8")

    boot = bootstrap(home=tmp_path)
    invalidations = invalidate_host_caches(boot.hosts, home=tmp_path)
    assert any(inv.host == "Hermes" and inv.succeeded for inv in invalidations)
    assert not snapshot.exists()


def test_render_bootstrap_summary_lists_each_host(tmp_path: Path) -> None:
    (tmp_path / ".hermes").mkdir()
    result = bootstrap(home=tmp_path, force=False)
    text = render_bootstrap_summary(result)
    assert "Hermes" in text
    assert "OpenClaw" in text
    assert "MCP configuration" in text


def test_cli_bootstrap_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_DOCTOR_HOST_HOME", str(tmp_path))
    (tmp_path / ".claude" / "skills").mkdir(parents=True)

    result = subprocess.run(
        [sys.executable, "-m", "agent_doctor.cli", "bootstrap"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "Agent Doctor — bootstrap" in result.stdout
    assert "Claude Code" in result.stdout
    assert (tmp_path / ".claude" / "skills" / "agent-doctor" / "SKILL.md").exists()


def test_cli_bootstrap_dry_run_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_DOCTOR_HOST_HOME", str(tmp_path))
    (tmp_path / ".claude" / "skills").mkdir(parents=True)

    subprocess.run(
        [sys.executable, "-m", "agent_doctor.cli", "bootstrap", "--dry-run"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert not (tmp_path / ".claude" / "skills" / "agent-doctor").exists()
