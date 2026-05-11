"""Tests for ``agent_doctor.usage`` and the ``pet-usage`` CLI.

No live ``npx`` calls. We monkey-patch :func:`subprocess.run` and
:func:`shutil.which` so the tests exercise the parsing + aggregation logic
end-to-end without spinning up Node. Boundary tests cover the Codex 5h /
7d windowing logic (the contract specifically calls out exactly-at /
before / after / empty cases).
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_doctor import usage as usage_mod
from agent_doctor.cli import main as cli_main


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------


def _completed(stdout: str, *, returncode: int = 0, stderr: str = "") -> SimpleNamespace:
    """Build a minimal stand-in for ``subprocess.CompletedProcess``."""

    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


class FakeRunner:
    """Map a fixture per (subcommand, package) so the four queries stay
    independently mockable. Tests register stdout payloads; we look up
    whichever the code under test asks for, defaulting to a benign exit-0
    response so unexpected calls don't crash before assertion."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.responses: dict[tuple[str, str], SimpleNamespace] = {}
        self.raise_for: dict[tuple[str, str], Exception] = {}

    def set(self, package: str, subcommand: str, response: SimpleNamespace) -> None:
        self.responses[(package, subcommand)] = response

    def raise_(self, package: str, subcommand: str, exc: Exception) -> None:
        self.raise_for[(package, subcommand)] = exc

    def __call__(self, cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        # ``capture_output=True, text=True`` is what the real implementation
        # passes; the test isn't asserting on those, so accept any kwargs.
        self.calls.append(list(cmd))
        # cmd = ["npx", "<package>", "<sub>", ...]
        if len(cmd) < 3 or cmd[0] != "npx":
            return _completed("", returncode=1, stderr="bad cmd")
        package, sub = cmd[1], cmd[2]
        key = (package, sub)
        if key in self.raise_for:
            raise self.raise_for[key]
        if key in self.responses:
            return self.responses[key]
        return _completed("{}", returncode=0)


@pytest.fixture
def fake_runner(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    runner = FakeRunner()
    monkeypatch.setattr(usage_mod.subprocess, "run", runner)
    monkeypatch.setattr(usage_mod.shutil, "which", lambda _: "/usr/local/bin/npx")
    return runner


def _claude_5h_payload(*, active: bool = True) -> str:
    return json.dumps(
        {
            "blocks": [
                {
                    "startTime": "2026-05-10T15:00:00Z",
                    "endTime": "2026-05-10T20:00:00Z",
                    "isActive": active,
                    "tokenCounts": {
                        "inputTokens": 4000,
                        "outputTokens": 2000,
                        "cacheCreationInputTokens": 1000,
                        "cacheReadInputTokens": 500,
                    },
                    "totalTokens": 7500,
                    "costUSD": 0.45,
                    "models": ["claude-opus-4-7", "claude-sonnet-4-6"],
                    "projection": {"remainingMinutes": 132},
                }
            ]
        }
    )


def _claude_weekly_payload() -> str:
    return json.dumps(
        {
            "weekly": [
                {
                    "week": "2026-04-27",
                    "totalTokens": 50000,
                    "totalCost": 3.21,
                    "modelsUsed": ["claude-sonnet-4-6"],
                    "modelBreakdowns": [
                        {
                            "inputTokens": 30000,
                            "outputTokens": 18000,
                            "cacheCreationInputTokens": 1500,
                            "cacheReadInputTokens": 500,
                        }
                    ],
                },
                {
                    "week": "2026-05-04",
                    "totalTokens": 80000,
                    "totalCost": 5.10,
                    "modelsUsed": ["claude-opus-4-7"],
                    "modelBreakdowns": [
                        {"inputTokens": 50000, "outputTokens": 28000},
                    ],
                },
            ]
        }
    )


def _codex_sessions_payload(now: datetime) -> str:
    """Codex sessions: one inside 5h, one boundary, one outside."""

    inside = (now - timedelta(hours=1)).isoformat()
    boundary = (now - timedelta(hours=5)).isoformat()
    outside = (now - timedelta(hours=12)).isoformat()
    return json.dumps(
        {
            "sessions": [
                {
                    "sessionId": "s-inside",
                    "lastActivity": inside,
                    "inputTokens": 1200,
                    "outputTokens": 800,
                    "totalCost": 0.05,
                    "models": ["gpt-5-codex"],
                },
                {
                    "sessionId": "s-boundary",
                    "lastActivity": boundary,
                    "inputTokens": 100,
                    "outputTokens": 200,
                    "totalCost": 0.01,
                    "models": ["gpt-5-codex"],
                },
                {
                    "sessionId": "s-outside",
                    "lastActivity": outside,
                    "inputTokens": 99999,
                    "outputTokens": 99999,
                    "totalCost": 99.0,
                    "models": ["gpt-5-codex"],
                },
            ]
        }
    )


def _codex_daily_payload(now: datetime) -> str:
    """Codex daily aggregates: one each at +0, +3d, +6d, +7d, +14d before now."""

    days = [0, 3, 6, 7, 14]
    entries = []
    for offset in days:
        date = (now - timedelta(days=offset)).date().isoformat()
        entries.append(
            {
                "date": date,
                "inputTokens": 1000 * (offset + 1),
                "outputTokens": 500 * (offset + 1),
                "totalCost": 0.10 * (offset + 1),
                "models": ["gpt-5-codex"],
            }
        )
    return json.dumps({"daily": entries})


# ---------------------------------------------------------------------------
#  Golden-path test: all four sources happy
# ---------------------------------------------------------------------------


def test_collect_usage_aggregates_all_four_sources(
    fake_runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 5, 10, 20, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(usage_mod, "_now_epoch", lambda: now.timestamp())
    monkeypatch.setattr(usage_mod, "_now_iso", lambda: now.isoformat())
    fake_runner.set(
        "ccusage@latest", "blocks", _completed(_claude_5h_payload())
    )
    fake_runner.set(
        "ccusage@latest", "weekly", _completed(_claude_weekly_payload())
    )
    fake_runner.set(
        "@ccusage/codex@latest", "session", _completed(_codex_sessions_payload(now))
    )
    fake_runner.set(
        "@ccusage/codex@latest", "daily", _completed(_codex_daily_payload(now))
    )

    payload = usage_mod.collect_usage(timeout=2.0)

    assert payload["generated_at"] == now.isoformat()
    # Claude 5h
    claude_5h = payload["claude"]["window_5h"]
    assert claude_5h is not None
    assert claude_5h["tokens"]["total"] == 7500
    assert claude_5h["tokens"]["input"] == 4000
    assert claude_5h["tokens"]["output"] == 2000
    assert claude_5h["tokens"]["cache"] == 1500
    assert claude_5h["cost_usd"] == pytest.approx(0.45)
    assert "claude-opus-4-7" in claude_5h["models"]
    assert claude_5h["reset_minutes"] == 132
    # Claude weekly: must use the latest entry
    claude_wk = payload["claude"]["window_weekly"]
    assert claude_wk is not None
    assert claude_wk["tokens"]["total"] == 80000
    assert claude_wk["cost_usd"] == pytest.approx(5.10)
    # Codex 5h: only the s-inside session (boundary is exactly-at, exclusive)
    codex_5h = payload["codex"]["window_5h"]
    assert codex_5h is not None
    assert codex_5h["tokens"]["input"] == 1200
    assert codex_5h["tokens"]["output"] == 800
    assert codex_5h["cost_usd"] == pytest.approx(0.05)
    # Codex weekly: 0, 3, 6 days are inside; 7 (boundary) and 14 are outside.
    codex_wk = payload["codex"]["window_weekly"]
    assert codex_wk is not None
    # offset+1 multiplier ⇒ inputs at 1000, 4000, 7000 = 12000
    assert codex_wk["tokens"]["input"] == 12000
    # outputs at 500, 2000, 3500 = 6000
    assert codex_wk["tokens"]["output"] == 6000
    # cost: 0.10 + 0.40 + 0.70 = 1.20
    assert codex_wk["cost_usd"] == pytest.approx(1.20)
    # Errors fields stay clean on the happy path.
    assert payload["claude"]["error"] is None
    assert payload["codex"]["error"] is None


# ---------------------------------------------------------------------------
#  Codex 5h boundary cases — exactly-at, before, after, empty
# ---------------------------------------------------------------------------


def test_codex_5h_boundary_exactly_at_is_excluded(
    fake_runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session whose lastActivity == cutoff is treated as outside the window.

    The contract reserves the 5h boundary for "active" sessions; a session
    that touched the cutoff to the second is conservatively excluded so the
    user can't see ghost numbers from a window that just rolled over.
    """

    now = datetime(2026, 5, 10, 20, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(usage_mod, "_now_epoch", lambda: now.timestamp())
    monkeypatch.setattr(usage_mod, "_now_iso", lambda: now.isoformat())
    boundary = (now - timedelta(hours=5)).isoformat()
    after = (now - timedelta(hours=6)).isoformat()
    fake_runner.set(
        "@ccusage/codex@latest",
        "session",
        _completed(
            json.dumps(
                {
                    "sessions": [
                        {"lastActivity": boundary, "inputTokens": 999},
                        {"lastActivity": after, "inputTokens": 888},
                    ]
                }
            )
        ),
    )
    # Avoid coupling to the other three sources for this case.
    fake_runner.set("ccusage@latest", "blocks", _completed("{}"))
    fake_runner.set("ccusage@latest", "weekly", _completed("{}"))
    fake_runner.set("@ccusage/codex@latest", "daily", _completed("{}"))

    payload = usage_mod.collect_usage(timeout=2.0)
    assert payload["codex"]["window_5h"] is None
    assert "no Codex sessions" in payload["codex"]["error"]


def test_codex_5h_session_just_before_cutoff_is_included(
    fake_runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 5, 10, 20, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(usage_mod, "_now_epoch", lambda: now.timestamp())
    just_before = (now - timedelta(hours=4, minutes=59)).isoformat()
    fake_runner.set(
        "@ccusage/codex@latest",
        "session",
        _completed(
            json.dumps(
                {
                    "sessions": [
                        {
                            "lastActivity": just_before,
                            "inputTokens": 100,
                            "outputTokens": 50,
                            "totalCost": 0.02,
                        },
                    ]
                }
            )
        ),
    )
    fake_runner.set("ccusage@latest", "blocks", _completed("{}"))
    fake_runner.set("ccusage@latest", "weekly", _completed("{}"))
    fake_runner.set("@ccusage/codex@latest", "daily", _completed("{}"))

    payload = usage_mod.collect_usage(timeout=2.0)
    assert payload["codex"]["window_5h"] is not None
    assert payload["codex"]["window_5h"]["tokens"]["input"] == 100


def test_codex_5h_empty_session_list(
    fake_runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 5, 10, 20, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(usage_mod, "_now_epoch", lambda: now.timestamp())
    fake_runner.set(
        "@ccusage/codex@latest", "session", _completed('{"sessions": []}')
    )
    fake_runner.set("ccusage@latest", "blocks", _completed("{}"))
    fake_runner.set("ccusage@latest", "weekly", _completed("{}"))
    fake_runner.set("@ccusage/codex@latest", "daily", _completed("{}"))

    payload = usage_mod.collect_usage(timeout=2.0)
    assert payload["codex"]["window_5h"] is None
    assert payload["codex"]["error"] is not None


def test_codex_weekly_seven_day_boundary(
    fake_runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 7-day boundary respects day granularity.

    "Last 7 days" = today + the 6 days immediately before. A daily entry
    whose date == ``now - 7d`` falls one day outside that window and must
    NOT count. The -6d entry (the oldest day still in the window) and
    today (-0d) both count.
    """

    now = datetime(2026, 5, 10, 14, 30, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(usage_mod, "_now_epoch", lambda: now.timestamp())
    monkeypatch.setattr(usage_mod, "_now_iso", lambda: now.isoformat())

    today_date = now.date().isoformat()
    inside_date = (now - timedelta(days=6)).date().isoformat()
    boundary_date = (now - timedelta(days=7)).date().isoformat()
    outside_date = (now - timedelta(days=14)).date().isoformat()
    fake_runner.set(
        "@ccusage/codex@latest",
        "daily",
        _completed(
            json.dumps(
                {
                    "daily": [
                        {"date": today_date, "inputTokens": 10, "outputTokens": 5, "totalCost": 0.005},
                        {"date": inside_date, "inputTokens": 50, "outputTokens": 25, "totalCost": 0.01},
                        {"date": boundary_date, "inputTokens": 9999, "outputTokens": 9999, "totalCost": 9.0},
                        {"date": outside_date, "inputTokens": 999, "outputTokens": 999, "totalCost": 1.0},
                    ]
                }
            )
        ),
    )
    fake_runner.set("ccusage@latest", "blocks", _completed("{}"))
    fake_runner.set("ccusage@latest", "weekly", _completed("{}"))
    fake_runner.set("@ccusage/codex@latest", "session", _completed("{}"))

    payload = usage_mod.collect_usage(timeout=2.0)
    weekly = payload["codex"]["window_weekly"]
    assert weekly is not None
    # today + -6d count; -7d and -14d are excluded.
    assert weekly["tokens"]["input"] == 10 + 50
    assert weekly["tokens"]["output"] == 5 + 25


# ---------------------------------------------------------------------------
#  Error paths — command-not-found, unparseable JSON, timeout, empty blocks
# ---------------------------------------------------------------------------


def test_collect_usage_handles_command_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usage_mod.shutil, "which", lambda _: None)
    # subprocess.run must not be called in this branch
    monkeypatch.setattr(
        usage_mod.subprocess,
        "run",
        lambda *a, **kw: pytest.fail("subprocess.run should be skipped when npx is missing"),
    )
    payload = usage_mod.collect_usage(timeout=1.0)
    assert payload["claude"]["window_5h"] is None
    assert payload["claude"]["window_weekly"] is None
    assert payload["codex"]["window_5h"] is None
    assert payload["codex"]["window_weekly"] is None
    assert "npx is not on PATH" in payload["claude"]["error"]
    assert "npx is not on PATH" in payload["codex"]["error"]


def test_collect_usage_handles_unparseable_json(fake_runner: FakeRunner) -> None:
    fake_runner.set("ccusage@latest", "blocks", _completed("not json {"))
    fake_runner.set("ccusage@latest", "weekly", _completed(_claude_weekly_payload()))
    fake_runner.set("@ccusage/codex@latest", "session", _completed("{}"))
    fake_runner.set("@ccusage/codex@latest", "daily", _completed("{}"))

    payload = usage_mod.collect_usage(timeout=2.0)
    assert payload["claude"]["window_5h"] is None
    assert "unparseable JSON" in payload["claude"]["error"]
    # weekly is fine, so it should still serialize
    assert payload["claude"]["window_weekly"] is not None


def test_collect_usage_handles_timeout(fake_runner: FakeRunner) -> None:
    fake_runner.raise_(
        "ccusage@latest",
        "blocks",
        subprocess.TimeoutExpired(cmd=["npx", "ccusage@latest", "blocks"], timeout=1.0),
    )
    payload = usage_mod.collect_usage(timeout=1.0)
    assert payload["claude"]["window_5h"] is None
    assert "timed out" in payload["claude"]["error"]


def test_collect_usage_handles_empty_blocks(fake_runner: FakeRunner) -> None:
    fake_runner.set("ccusage@latest", "blocks", _completed('{"blocks": []}'))
    fake_runner.set("ccusage@latest", "weekly", _completed(_claude_weekly_payload()))
    fake_runner.set("@ccusage/codex@latest", "session", _completed("{}"))
    fake_runner.set("@ccusage/codex@latest", "daily", _completed("{}"))

    payload = usage_mod.collect_usage(timeout=2.0)
    assert payload["claude"]["window_5h"] is None
    assert "no active 5h block" in payload["claude"]["error"]


def test_collect_usage_handles_nonzero_exit(fake_runner: FakeRunner) -> None:
    fake_runner.set(
        "ccusage@latest",
        "blocks",
        _completed("", returncode=2, stderr="npx: package not found"),
    )
    fake_runner.set("ccusage@latest", "weekly", _completed(_claude_weekly_payload()))
    fake_runner.set("@ccusage/codex@latest", "session", _completed("{}"))
    fake_runner.set("@ccusage/codex@latest", "daily", _completed("{}"))

    payload = usage_mod.collect_usage(timeout=2.0)
    assert payload["claude"]["window_5h"] is None
    assert "package not found" in payload["claude"]["error"]


def test_collect_usage_never_raises_on_unexpected_exception(
    fake_runner: FakeRunner,
) -> None:
    fake_runner.raise_("ccusage@latest", "blocks", RuntimeError("kaboom"))
    fake_runner.set("ccusage@latest", "weekly", _completed("{}"))
    fake_runner.set("@ccusage/codex@latest", "session", _completed("{}"))
    fake_runner.set("@ccusage/codex@latest", "daily", _completed("{}"))

    # Must not raise.
    payload = usage_mod.collect_usage(timeout=1.0)
    assert payload["claude"]["window_5h"] is None


# ---------------------------------------------------------------------------
#  CLI integration
# ---------------------------------------------------------------------------


def test_cli_pet_usage_json_emits_payload(
    fake_runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime(2026, 5, 10, 20, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(usage_mod, "_now_epoch", lambda: now.timestamp())
    monkeypatch.setattr(usage_mod, "_now_iso", lambda: now.isoformat())
    fake_runner.set("ccusage@latest", "blocks", _completed(_claude_5h_payload()))
    fake_runner.set("ccusage@latest", "weekly", _completed(_claude_weekly_payload()))
    fake_runner.set(
        "@ccusage/codex@latest", "session", _completed(_codex_sessions_payload(now))
    )
    fake_runner.set(
        "@ccusage/codex@latest", "daily", _completed(_codex_daily_payload(now))
    )

    exit_code = cli_main(["pet-usage", "--json"])
    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["claude"]["window_5h"]["tokens"]["total"] == 7500
    assert payload["codex"]["window_weekly"]["cost_usd"] > 0


def test_cli_pet_usage_exit_zero_with_error_when_npx_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(usage_mod.shutil, "which", lambda _: None)
    monkeypatch.setattr(
        usage_mod.subprocess,
        "run",
        lambda *a, **kw: pytest.fail("subprocess.run should be skipped"),
    )
    exit_code = cli_main(["pet-usage", "--json"])
    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["claude"]["window_5h"] is None
    assert payload["claude"]["error"] is not None
    assert payload["codex"]["error"] is not None


# ---------------------------------------------------------------------------
#  Swift source assertions — keep the click handler + popover + warmup wired
# ---------------------------------------------------------------------------


SWIFT_SOURCE = Path(__file__).parent.parent / "agent_doctor" / "assets" / "pet_display.swift"


def test_swift_source_has_nspopover() -> None:
    src = SWIFT_SOURCE.read_text(encoding="utf-8")
    assert "NSPopover" in src, "popover class must be present"
    assert ".transient" in src, "popover must use transient behavior"
    assert ".maxX" in src, "popover must anchor to the right edge"


def test_swift_source_has_click_vs_drag_threshold() -> None:
    src = SWIFT_SOURCE.read_text(encoding="utf-8")
    # Threshold values are part of the contract; if a future edit changes
    # them, this test fails loudly so the docs + verifier stay in sync.
    assert "displacement < 4.0" in src, "click threshold must be 4pt"
    assert "elapsed < 0.4" in src, "click threshold must be 400ms"
    assert "clickStartedAt" in src
    assert "clickStartPoint" in src


def test_swift_source_calls_pet_usage_cli() -> None:
    src = SWIFT_SOURCE.read_text(encoding="utf-8")
    assert "openUsagePopover" in src
    assert '"pet-usage"' in src
    assert '"--json"' in src
    assert "agent_doctor.cli" in src


def test_swift_source_warms_up_npx_in_bootstrap() -> None:
    src = SWIFT_SOURCE.read_text(encoding="utf-8")
    assert "warmupNpxPackages" in src
    # The warmup must be *called* after the window is shown so the user
    # never sees a startup delay. The function declaration sits near the
    # top of the file, so we search for the call site by slicing past the
    # bootstrap line and looking for the next occurrence of the call.
    bootstrap_index = src.index("window.makeKeyAndOrderFront(nil)")
    tail = src[bootstrap_index:]
    assert "warmupNpxPackages()" in tail, "warmup must be called in bootstrap"
    assert "ccusage@latest" in src
    assert "@ccusage/codex@latest" in src


def test_swift_source_keeps_right_click_menu_intact() -> None:
    src = SWIFT_SOURCE.read_text(encoding="utf-8")
    assert "rightMouseDown" in src
    assert "popUpContextMenu" in src


def test_swift_source_preserves_drag_behavior() -> None:
    src = SWIFT_SOURCE.read_text(encoding="utf-8")
    # The drag guard remains the first thing mouseUp checks.
    assert "if isDragging {\n            return\n        }" in src


# ---------------------------------------------------------------------------
#  Unit checks on the coercion helpers (defensive against bad ccusage shapes)
# ---------------------------------------------------------------------------


def test_coerce_int_handles_strings_and_floats() -> None:
    assert usage_mod._coerce_int("42") == 42
    assert usage_mod._coerce_int("not a number") == 0
    assert usage_mod._coerce_int(3.7) == 3
    assert usage_mod._coerce_int(None) == 0
    assert usage_mod._coerce_int(True) == 0  # bools are intentionally rejected


def test_coerce_float_handles_strings_and_ints() -> None:
    assert usage_mod._coerce_float("1.25") == 1.25
    assert usage_mod._coerce_float("nope") == 0.0
    assert usage_mod._coerce_float(5) == 5.0
    assert usage_mod._coerce_float(None) == 0.0


def test_normalize_iso_round_trips_zulu() -> None:
    assert usage_mod._normalize_iso("2026-05-10T20:00:00Z").startswith("2026-05-10T20:00:00")


def test_parse_iso_to_epoch_accepts_iso_and_epoch_seconds() -> None:
    epoch = usage_mod._parse_iso_to_epoch("2026-05-10T20:00:00Z")
    assert epoch is not None
    assert int(epoch) == int(
        datetime(2026, 5, 10, 20, 0, 0, tzinfo=timezone.utc).timestamp()
    )
    assert usage_mod._parse_iso_to_epoch(1_715_000_000) == 1_715_000_000
    assert usage_mod._parse_iso_to_epoch(None) is None
    assert usage_mod._parse_iso_to_epoch("garbage") is None
