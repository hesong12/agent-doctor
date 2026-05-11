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


def _codex_daily_payload(now: datetime) -> str:
    """Codex daily aggregates spread across the ISO week containing ``now``.

    Generates one entry per day offset ``[0, 3, 6, 7, 14]`` measured back
    from ``now``. With ``now = 2026-05-10`` (Sun) the current ISO week is
    Mon ``2026-05-04`` → Mon ``2026-05-11`` (exclusive), so offsets
    ``{0, 3, 6}`` (= ``05-10, 05-07, 05-04``) land inside the week and
    ``{7, 14}`` (= ``05-03, 04-26``) land outside it. Today's entry is the
    offset-0 row.
    """

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
    """Happy-path: every source returns data, every card lands populated.

    ``now = 2026-05-10T20:00:00Z`` (Sunday). Current ISO week is Mon
    ``2026-05-04`` → Mon ``2026-05-11`` (exclusive); current ISO day is
    ``2026-05-10``. Claude weekly's fixture entry for week ``2026-05-04``
    matches the current Monday and Codex daily's offset-0 entry lands on
    today.
    """

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
    # Claude weekly: filter to the entry matching the current ISO Monday
    # (2026-05-04). The 2026-04-27 entry must NOT be picked.
    claude_wk = payload["claude"]["window_weekly"]
    assert claude_wk is not None
    assert claude_wk["tokens"]["total"] == 80000
    assert claude_wk["cost_usd"] == pytest.approx(5.10)
    assert claude_wk["start_iso"].startswith("2026-05-04T00:00:00")
    assert claude_wk["end_iso"].startswith("2026-05-11T00:00:00")
    # Codex today: only the offset-0 daily entry (date == 2026-05-10).
    codex_today = payload["codex"]["window_today"]
    assert codex_today is not None
    assert codex_today["tokens"]["input"] == 1000
    assert codex_today["tokens"]["output"] == 500
    assert codex_today["cost_usd"] == pytest.approx(0.10)
    assert codex_today["start_iso"].startswith("2026-05-10T00:00:00")
    assert codex_today["end_iso"].startswith("2026-05-11T00:00:00")
    # Codex weekly: 0, 3, 6 days are inside the ISO week; 7 (Sun May 3) and
    # 14 are outside. offset+1 multiplier ⇒ inputs at 1000+4000+7000 = 12000.
    codex_wk = payload["codex"]["window_weekly"]
    assert codex_wk is not None
    assert codex_wk["tokens"]["input"] == 12000
    assert codex_wk["tokens"]["output"] == 6000          # 500 + 2000 + 3500
    assert codex_wk["cost_usd"] == pytest.approx(1.20)   # 0.10 + 0.40 + 0.70
    assert codex_wk["start_iso"].startswith("2026-05-04T00:00:00")
    assert codex_wk["end_iso"].startswith("2026-05-11T00:00:00")
    # Errors fields stay clean on the happy path.
    assert payload["claude"]["error"] is None
    assert payload["codex"]["error"] is None


# ---------------------------------------------------------------------------
#  Codex calendar-window boundary cases (ISO day + ISO week)
# ---------------------------------------------------------------------------


def test_codex_weekly_uses_iso_week_boundary(
    fake_runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Aggregation respects the ISO week (Mon→Mon UTC), not "last 7 days".

    Pin ``now`` to mid-Wednesday so today is inside the week and last
    Sunday is outside. With ``now = 2026-05-06T14:30Z`` (Wed) the current
    ISO week is ``[2026-05-04, 2026-05-11)``. A row dated ``2026-05-03``
    (Sun before) must NOT count even though it's only 3 days ago; a row
    dated the current ISO Monday must count.
    """

    now = datetime(2026, 5, 6, 14, 30, 0, tzinfo=timezone.utc)  # Wednesday
    monkeypatch.setattr(usage_mod, "_now_epoch", lambda: now.timestamp())
    monkeypatch.setattr(usage_mod, "_now_iso", lambda: now.isoformat())

    fake_runner.set(
        "@ccusage/codex@latest",
        "daily",
        _completed(
            json.dumps(
                {
                    "daily": [
                        # Inside the ISO week (Mon..Wed).
                        {"date": "2026-05-04", "inputTokens": 50, "outputTokens": 25, "totalCost": 0.01},
                        {"date": "2026-05-06", "inputTokens": 10, "outputTokens": 5, "totalCost": 0.005},
                        # Outside: previous-week Sunday and a row 14 days back.
                        {"date": "2026-05-03", "inputTokens": 9999, "outputTokens": 9999, "totalCost": 9.0},
                        {"date": "2026-04-22", "inputTokens": 999, "outputTokens": 999, "totalCost": 1.0},
                    ]
                }
            )
        ),
    )
    fake_runner.set("ccusage@latest", "blocks", _completed("{}"))
    fake_runner.set("ccusage@latest", "weekly", _completed("{}"))

    payload = usage_mod.collect_usage(timeout=2.0)
    weekly = payload["codex"]["window_weekly"]
    assert weekly is not None
    assert weekly["tokens"]["input"] == 50 + 10
    assert weekly["tokens"]["output"] == 25 + 5
    assert weekly["start_iso"].startswith("2026-05-04T00:00:00")
    assert weekly["end_iso"].startswith("2026-05-11T00:00:00")


def test_codex_today_picks_only_todays_entry(
    fake_runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex today: only the row whose ``date`` equals today's calendar day.

    Yesterday's row (which the old rolling-5h logic might have caught)
    must be excluded — the spec re-anchors to the calendar day.
    """

    now = datetime(2026, 5, 10, 14, 30, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(usage_mod, "_now_epoch", lambda: now.timestamp())
    monkeypatch.setattr(usage_mod, "_now_iso", lambda: now.isoformat())

    fake_runner.set(
        "@ccusage/codex@latest",
        "daily",
        _completed(
            json.dumps(
                {
                    "daily": [
                        {"date": "2026-05-10", "inputTokens": 77, "outputTokens": 33, "totalCost": 0.07},
                        {"date": "2026-05-09", "inputTokens": 9999, "outputTokens": 9999, "totalCost": 9.0},
                    ]
                }
            )
        ),
    )
    fake_runner.set("ccusage@latest", "blocks", _completed("{}"))
    fake_runner.set("ccusage@latest", "weekly", _completed("{}"))

    payload = usage_mod.collect_usage(timeout=2.0)
    today = payload["codex"]["window_today"]
    assert today is not None
    assert today["tokens"]["input"] == 77
    assert today["tokens"]["output"] == 33
    assert today["cost_usd"] == pytest.approx(0.07)
    assert today["start_iso"].startswith("2026-05-10T00:00:00")
    assert today["end_iso"].startswith("2026-05-11T00:00:00")


def test_codex_today_returns_empty_window_when_no_entry_for_today(
    fake_runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty-data day: ``window_today`` is still emitted with calendar bounds
    and all-zero tokens / cost / models, so the popover renders a real
    "0 tokens, X% elapsed" card instead of a "no data" fallback.
    """

    now = datetime(2026, 5, 10, 6, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(usage_mod, "_now_epoch", lambda: now.timestamp())
    monkeypatch.setattr(usage_mod, "_now_iso", lambda: now.isoformat())

    fake_runner.set(
        "@ccusage/codex@latest",
        "daily",
        _completed(json.dumps({"daily": [
            {"date": "2026-05-09", "inputTokens": 100, "totalCost": 0.1},
        ]})),
    )
    fake_runner.set("ccusage@latest", "blocks", _completed("{}"))
    fake_runner.set("ccusage@latest", "weekly", _completed("{}"))

    payload = usage_mod.collect_usage(timeout=2.0)
    today = payload["codex"]["window_today"]
    assert today is not None
    assert today["tokens"]["total"] == 0
    assert today["cost_usd"] == 0.0
    assert today["models"] == []
    assert today["start_iso"].startswith("2026-05-10T00:00:00")
    assert today["end_iso"].startswith("2026-05-11T00:00:00")
    # Calendar-derived progress is still meaningful: 6h elapsed of 24h = 25%.
    assert today["elapsed_pct"] == pytest.approx(25.0)
    assert today["remaining_minutes"] == 1080  # 18h
    assert today["remaining_human"] == "18h"


def test_claude_weekly_returns_empty_window_when_no_matching_iso_monday(
    fake_runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty-data week: when ccusage has no row for the current ISO Monday
    (e.g. fresh Monday morning UTC with no usage logged yet) we still emit
    a calendar-bounded zero window."""

    now = datetime(2026, 5, 11, 8, 0, 0, tzinfo=timezone.utc)  # Mon morning
    monkeypatch.setattr(usage_mod, "_now_epoch", lambda: now.timestamp())
    monkeypatch.setattr(usage_mod, "_now_iso", lambda: now.isoformat())

    fake_runner.set(
        "ccusage@latest",
        "weekly",
        _completed(
            json.dumps(
                {
                    "weekly": [
                        # Only last-week and prior-week data — no current-week row.
                        {"week": "2026-05-04", "totalTokens": 50000, "totalCost": 3.21},
                        {"week": "2026-04-27", "totalTokens": 12000, "totalCost": 0.75},
                    ]
                }
            )
        ),
    )
    fake_runner.set("ccusage@latest", "blocks", _completed("{}"))
    fake_runner.set("@ccusage/codex@latest", "daily", _completed("{}"))

    payload = usage_mod.collect_usage(timeout=2.0)
    weekly = payload["claude"]["window_weekly"]
    assert weekly is not None
    assert weekly["tokens"]["total"] == 0
    assert weekly["cost_usd"] == 0.0
    assert weekly["models"] == []
    assert weekly["start_iso"].startswith("2026-05-11T00:00:00")
    assert weekly["end_iso"].startswith("2026-05-18T00:00:00")
    # 8h elapsed of 168h ≈ 4.8%.
    assert weekly["elapsed_pct"] == pytest.approx(4.8)


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
    assert payload["codex"]["window_today"] is None
    assert payload["codex"]["window_weekly"] is None
    assert "npx is not on PATH" in payload["claude"]["error"]
    assert "npx is not on PATH" in payload["codex"]["error"]


def test_collect_usage_handles_unparseable_json(fake_runner: FakeRunner) -> None:
    fake_runner.set("ccusage@latest", "blocks", _completed("not json {"))
    fake_runner.set("ccusage@latest", "weekly", _completed(_claude_weekly_payload()))
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
    fake_runner.set("@ccusage/codex@latest", "daily", _completed("{}"))

    payload = usage_mod.collect_usage(timeout=2.0)
    assert payload["claude"]["window_5h"] is None
    assert "package not found" in payload["claude"]["error"]


def test_collect_usage_never_raises_on_unexpected_exception(
    fake_runner: FakeRunner,
) -> None:
    fake_runner.raise_("ccusage@latest", "blocks", RuntimeError("kaboom"))
    fake_runner.set("ccusage@latest", "weekly", _completed("{}"))
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
        "@ccusage/codex@latest", "daily", _completed(_codex_daily_payload(now))
    )

    exit_code = cli_main(["pet-usage", "--json"])
    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["claude"]["window_5h"]["tokens"]["total"] == 7500
    assert payload["codex"]["window_today"]["tokens"]["total"] > 0
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


def test_iso_for_fromisoformat_handles_z_suffix() -> None:
    """Single helper normalizes the ``Z`` UTC suffix.

    Python 3.10 raised ``ValueError`` for ``"2026-05-10T00:00:00Z"``,
    and even on 3.11+ keeping all ``fromisoformat`` callers consistent
    prevents a future caller (e.g. the week-start parser) from quietly
    rejecting an upstream timestamp that gained a ``Z`` suffix between
    ccusage releases.
    """

    assert usage_mod._iso_for_fromisoformat("2026-05-10T00:00:00Z") == "2026-05-10T00:00:00+00:00"
    # No-op when the value is already ``+00:00``.
    assert usage_mod._iso_for_fromisoformat("2026-05-10T00:00:00+00:00") == "2026-05-10T00:00:00+00:00"
    # Whitespace is stripped.
    assert usage_mod._iso_for_fromisoformat("  2026-05-10  ") == "2026-05-10"


def test_find_weekly_entry_matches_z_suffix_week_field() -> None:
    """``ccusage weekly`` historically emits a bare-date ``week`` field
    (``"2026-05-04"``), but a future version could append a time/zone
    suffix (``"2026-05-04T00:00:00Z"``). ``_find_weekly_entry`` matches by
    leading ``YYYY-MM-DD`` so both shapes route to the same row.
    """

    entries = [
        {"week": "2026-05-04T00:00:00Z", "totalTokens": 100},
        {"week": "2026-04-27", "totalTokens": 0},
    ]
    match = usage_mod._find_weekly_entry(entries, "2026-05-04")
    assert match is not None
    assert match["totalTokens"] == 100
    # Wrong key returns None — the caller falls back to an empty window.
    assert usage_mod._find_weekly_entry(entries, "2026-05-11") is None


def test_parse_codex_date_handles_real_us_format() -> None:
    """The live ``@ccusage/codex daily`` endpoint emits dates as
    ``"May 02, 2026"``. Parsing must succeed or the entire 7-day
    rollup silently returns no entries.
    """

    parsed = usage_mod._parse_codex_date("May 02, 2026")
    assert parsed is not None
    assert parsed.year == 2026 and parsed.month == 5 and parsed.day == 2
    # ISO fallback still works.
    iso = usage_mod._parse_codex_date("2026-05-02")
    assert iso is not None and iso.day == 2
    # Garbage stays None.
    assert usage_mod._parse_codex_date(None) is None
    assert usage_mod._parse_codex_date("not a date") is None


def test_codex_real_daily_schema_aggregates(
    fake_runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end against the live ``@ccusage/codex daily`` shape.

    Uses ``"May 02, 2026"`` dates, ``costUSD`` instead of ``totalCost``,
    and a model *dict* instead of a list — matches the v18.0.11
    schema that PR #20's follow-up nailed down. Failing this test
    would mean the popover silently drops the codex weekly window
    in production.
    """

    now = datetime(2026, 5, 10, 14, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(usage_mod, "_now_epoch", lambda: now.timestamp())
    monkeypatch.setattr(usage_mod, "_now_iso", lambda: now.isoformat())

    fake_runner.set(
        "@ccusage/codex@latest",
        "daily",
        _completed(
            json.dumps(
                {
                    "daily": [
                        {
                            # Inside the current ISO week (Mon May 4 .. Sun May 10).
                            "date": "May 09, 2026",
                            "inputTokens": 1000,
                            "cachedInputTokens": 200,
                            "outputTokens": 500,
                            "reasoningOutputTokens": 50,
                            "totalTokens": 1500,
                            "costUSD": 0.42,
                            "models": {"gpt-5.5": {"inputTokens": 1000}},
                        },
                        {
                            # Outside the current ISO week.
                            "date": "Apr 26, 2026",
                            "inputTokens": 9999,
                            "outputTokens": 9999,
                            "totalTokens": 19998,
                            "costUSD": 99.0,
                            "models": {"gpt-5.4": {}},
                        },
                    ]
                }
            )
        ),
    )
    fake_runner.set("ccusage@latest", "blocks", _completed("{}"))
    fake_runner.set("ccusage@latest", "weekly", _completed("{}"))

    payload = usage_mod.collect_usage(timeout=2.0)
    weekly = payload["codex"]["window_weekly"]
    assert weekly is not None
    assert weekly["cost_usd"] == pytest.approx(0.42)
    assert weekly["tokens"]["input"] == 1000
    assert weekly["tokens"]["output"] == 500
    # ``cachedInputTokens`` (singular) must map to the cache bucket.
    assert weekly["tokens"]["cache"] == 200
    assert weekly["models"] == ["gpt-5.5"]


def test_parse_iso_to_epoch_accepts_iso_and_epoch_seconds() -> None:
    epoch = usage_mod._parse_iso_to_epoch("2026-05-10T20:00:00Z")
    assert epoch is not None
    assert int(epoch) == int(
        datetime(2026, 5, 10, 20, 0, 0, tzinfo=timezone.utc).timestamp()
    )
    assert usage_mod._parse_iso_to_epoch(1_715_000_000) == 1_715_000_000
    assert usage_mod._parse_iso_to_epoch(None) is None
    assert usage_mod._parse_iso_to_epoch("garbage") is None


# ---------------------------------------------------------------------------
#  Window-progress helpers (_format_remaining, _compute_progress)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "minutes, expected",
    [
        # < 60 → "Xm"
        (0, "0m"),
        (59, "59m"),
        # 60-1439 → "Hh Mm", suppress trailing " 0m" → just "Hh"
        (60, "1h"),
        (61, "1h 1m"),
        (119, "1h 59m"),
        (120, "2h"),
        (1439, "23h 59m"),
        # >= 1440 → "Dd Hh", suppress trailing " 0h" → just "Dd"
        (1440, "1d"),
        (1441, "1d"),    # 1d + 0h + 1m → hours==0, so just "1d"
        (1500, "1d 1h"),
    ],
)
def test_format_remaining_matrix(minutes: int, expected: str) -> None:
    """Locked-in formatting contract for the popover's 'T left' string."""

    assert usage_mod._format_remaining(minutes) == expected


def test_compute_progress_just_started() -> None:
    """At ``now == start``, the window is 0% elapsed and the full span is left."""

    start = datetime(2026, 5, 10, 15, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 5, 10, 20, 0, 0, tzinfo=timezone.utc)  # 5h span = 300 min
    pct, mins, human = usage_mod._compute_progress(
        start.isoformat(),
        end.isoformat(),
        now_epoch=start.timestamp(),
    )
    assert pct == 0.0
    assert mins == 300
    assert human == "5h"


def test_compute_progress_halfway() -> None:
    start = datetime(2026, 5, 10, 15, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 5, 10, 20, 0, 0, tzinfo=timezone.utc)
    now_epoch = (start + timedelta(minutes=150)).timestamp()
    pct, mins, human = usage_mod._compute_progress(
        start.isoformat(), end.isoformat(), now_epoch=now_epoch
    )
    assert pct == 50.0
    assert mins == 150
    assert human == "2h 30m"


def test_compute_progress_almost_done() -> None:
    start = datetime(2026, 5, 10, 15, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 5, 10, 20, 0, 0, tzinfo=timezone.utc)
    now_epoch = (start + timedelta(minutes=297)).timestamp()
    pct, mins, human = usage_mod._compute_progress(
        start.isoformat(), end.isoformat(), now_epoch=now_epoch
    )
    assert pct == 99.0
    assert mins == 3
    assert human == "3m"


def test_compute_progress_past_end_clamps_to_100_and_zero() -> None:
    """A window that has rolled out clamps to ``100.0%`` elapsed + ``0`` remaining
    rather than reporting ``120%`` or a negative remainder."""

    start = datetime(2026, 5, 10, 15, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 5, 10, 20, 0, 0, tzinfo=timezone.utc)
    now_epoch = (end + timedelta(minutes=60)).timestamp()
    pct, mins, human = usage_mod._compute_progress(
        start.isoformat(), end.isoformat(), now_epoch=now_epoch
    )
    assert pct == 100.0
    assert mins == 0
    assert human == "0m"


def test_compute_progress_missing_bounds_signals_suppression() -> None:
    """Missing / unparseable bounds → ``(0.0, 0, '')``. The empty string is the
    signal the Swift popover uses to *suppress* the progress row entirely
    (otherwise a stale popover would render "0% elapsed (— left)" for a
    window whose bounds we couldn't read)."""

    for start_iso, end_iso in [
        ("", ""),
        ("not iso", "also not"),
        ("2026-05-10T15:00:00Z", ""),
        ("", "2026-05-10T20:00:00Z"),
        # end <= start collapses to "no usable bounds" too
        ("2026-05-10T20:00:00Z", "2026-05-10T15:00:00Z"),
    ]:
        pct, mins, human = usage_mod._compute_progress(
            start_iso, end_iso, now_epoch=0.0
        )
        assert pct == 0.0
        assert mins == 0
        assert human == ""


def test_compute_progress_reset_minutes_override_beats_wall_clock() -> None:
    """Claude 5h carries ccusage's ``projection.remainingMinutes``, which
    is more authoritative than ``endTime - now`` because ccusage knows
    ``actualEndTime``. The override applies to ``remaining_minutes`` /
    ``remaining_human`` only; ``elapsed_pct`` still comes from start/end.
    """

    start = datetime(2026, 5, 10, 15, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 5, 10, 20, 0, 0, tzinfo=timezone.utc)
    now_epoch = (start + timedelta(minutes=150)).timestamp()  # halfway by wall clock
    pct, mins, human = usage_mod._compute_progress(
        start.isoformat(),
        end.isoformat(),
        now_epoch=now_epoch,
        reset_minutes_override=132,
    )
    assert pct == 50.0          # still computed from start/end
    assert mins == 132           # override beats the wall-clock 150
    assert human == "2h 12m"


def test_collect_usage_includes_window_progress_on_every_window(
    fake_runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: ``pet-usage --json`` must surface ``elapsed_pct``,
    ``remaining_minutes``, and ``remaining_human`` on every populated
    window when source data is available, and Claude 5h must honor
    ``reset_minutes``.
    """

    now = datetime(2026, 5, 10, 20, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(usage_mod, "_now_epoch", lambda: now.timestamp())
    monkeypatch.setattr(usage_mod, "_now_iso", lambda: now.isoformat())
    fake_runner.set("ccusage@latest", "blocks", _completed(_claude_5h_payload()))
    fake_runner.set("ccusage@latest", "weekly", _completed(_claude_weekly_payload()))
    fake_runner.set(
        "@ccusage/codex@latest", "daily", _completed(_codex_daily_payload(now))
    )

    payload = usage_mod.collect_usage(timeout=2.0)
    # Window-key layout is asymmetric: claude has a 5h billing block while
    # codex now has a calendar-day "today" card. Both share the weekly slot.
    populated_slots = [
        ("claude", "window_5h"),
        ("claude", "window_weekly"),
        ("codex", "window_today"),
        ("codex", "window_weekly"),
    ]
    for branch_key, window_key in populated_slots:
        window = payload[branch_key][window_key]
        assert window is not None, f"{branch_key}.{window_key} should be populated"
        assert "elapsed_pct" in window
        assert "remaining_minutes" in window
        assert "remaining_human" in window
        assert isinstance(window["elapsed_pct"], float)
        assert 0.0 <= window["elapsed_pct"] <= 100.0
        assert isinstance(window["remaining_minutes"], int)
        assert window["remaining_minutes"] >= 0
        assert isinstance(window["remaining_human"], str)
        assert window["remaining_human"] != ""
    # Claude 5h must honor ccusage's projection.remainingMinutes (132 from
    # the fixture) rather than recomputing from endTime.
    assert payload["claude"]["window_5h"]["remaining_minutes"] == 132
    assert payload["claude"]["window_5h"]["remaining_human"] == "2h 12m"


def test_swift_source_renders_window_progress_row() -> None:
    """The card-builder reads ``elapsed_pct`` + ``remaining_human`` from the
    JSON payload, renders them as ``"<label>: X% elapsed (<human> left)"``,
    and uses ``"5h window"`` / ``"Today"`` / ``"Week"`` as the labels for
    the three card families. Mirrors the Swift-source assertion pattern
    used elsewhere in this file.
    """

    src = SWIFT_SOURCE.read_text(encoding="utf-8")
    assert "remaining_human" in src, "card must read remaining_human from payload"
    assert "elapsed_pct" in src, "card must read elapsed_pct from payload"
    assert "elapsed" in src, "card must render 'elapsed' in the user-visible label"
    assert '"5h window"' in src, "5h card must pass the '5h window' progress label"
    assert '"Today"' in src, "today card must pass the 'Today' progress label"
    assert '"Week"' in src, "weekly cards must pass the 'Week' progress label"
    # Card-builder helper takes a progressLabel and renders via formatElapsedPct.
    assert "progressLabel" in src
    assert "formatElapsedPct" in src


def test_swift_source_renames_codex_5h_to_codex_today() -> None:
    """The third card was relabeled from 'Codex · 5h' (rolling-lookback) to
    'Codex · today' (calendar day). The Swift renderer must read the new
    JSON key ``window_today`` and the old ``"Codex · 5h"`` literal must be
    fully gone — leaving the old string in would mean a stale render path
    is still wired up.
    """

    src = SWIFT_SOURCE.read_text(encoding="utf-8")
    assert "Codex · today" in src, "third card title must read 'Codex · today'"
    assert "Codex · 5h" not in src, "stale 'Codex · 5h' title must be removed"
    assert "window_today" in src, "card must read the renamed JSON key"


# ---------------------------------------------------------------------------
#  Calendar-window helpers: current_iso_week_window / current_iso_day_window
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "frozen_now, expected_start, expected_end",
    [
        # Monday morning — boundary case, today IS the Monday.
        (
            datetime(2026, 5, 4, 8, 0, 0, tzinfo=timezone.utc),
            "2026-05-04T00:00:00+00:00",
            "2026-05-11T00:00:00+00:00",
        ),
        # Mid-week Wednesday.
        (
            datetime(2026, 5, 6, 14, 30, 0, tzinfo=timezone.utc),
            "2026-05-04T00:00:00+00:00",
            "2026-05-11T00:00:00+00:00",
        ),
        # Sunday night, last second of the ISO week.
        (
            datetime(2026, 5, 10, 23, 59, 59, tzinfo=timezone.utc),
            "2026-05-04T00:00:00+00:00",
            "2026-05-11T00:00:00+00:00",
        ),
        # Year boundary — Wed 2026-12-30 (ISO Mon = 2026-12-28).
        (
            datetime(2026, 12, 30, 12, 0, 0, tzinfo=timezone.utc),
            "2026-12-28T00:00:00+00:00",
            "2027-01-04T00:00:00+00:00",
        ),
    ],
)
def test_current_iso_week_window_matrix(
    frozen_now: datetime, expected_start: str, expected_end: str
) -> None:
    start, end = usage_mod.current_iso_week_window(now=frozen_now)
    assert start == expected_start
    assert end == expected_end


@pytest.mark.parametrize(
    "frozen_now, expected_start, expected_end",
    [
        # Mid-day.
        (
            datetime(2026, 5, 10, 14, 30, 0, tzinfo=timezone.utc),
            "2026-05-10T00:00:00+00:00",
            "2026-05-11T00:00:00+00:00",
        ),
        # Just-past-midnight — must roll to the new day immediately.
        (
            datetime(2026, 5, 11, 0, 0, 1, tzinfo=timezone.utc),
            "2026-05-11T00:00:00+00:00",
            "2026-05-12T00:00:00+00:00",
        ),
        # Year boundary.
        (
            datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
            "2026-12-31T00:00:00+00:00",
            "2027-01-01T00:00:00+00:00",
        ),
    ],
)
def test_current_iso_day_window_matrix(
    frozen_now: datetime, expected_start: str, expected_end: str
) -> None:
    start, end = usage_mod.current_iso_day_window(now=frozen_now)
    assert start == expected_start
    assert end == expected_end


def test_calendar_window_helpers_default_to_now_epoch_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``now`` is omitted, both helpers must read the wall clock
    through ``_now_epoch`` so tests that monkey-patch the seam stay
    deterministic (this is how ``collect_usage`` derives its windows).
    """

    frozen = datetime(2026, 5, 6, 14, 30, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(usage_mod, "_now_epoch", lambda: frozen.timestamp())

    week_start, week_end = usage_mod.current_iso_week_window()
    day_start, day_end = usage_mod.current_iso_day_window()

    assert week_start == "2026-05-04T00:00:00+00:00"
    assert week_end == "2026-05-11T00:00:00+00:00"
    assert day_start == "2026-05-06T00:00:00+00:00"
    assert day_end == "2026-05-07T00:00:00+00:00"


def test_empty_window_factory_emits_calendar_bounds() -> None:
    """``_empty_window`` carries the calendar bounds verbatim and zeros out
    everything else, including ``models`` — important because the Swift
    card renders zero usage as ``"0 tokens • $0.00"`` and we don't want a
    stale model list from a previous week leaking through.
    """

    window = usage_mod._empty_window(
        start_iso="2026-05-04T00:00:00+00:00",
        end_iso="2026-05-11T00:00:00+00:00",
    )
    payload = window.to_dict(
        now_epoch=datetime(2026, 5, 6, 0, 0, 0, tzinfo=timezone.utc).timestamp()
    )
    assert payload["tokens"] == {"input": 0, "output": 0, "cache": 0, "total": 0}
    assert payload["cost_usd"] == 0.0
    assert payload["models"] == []
    assert payload["start_iso"] == "2026-05-04T00:00:00+00:00"
    assert payload["end_iso"] == "2026-05-11T00:00:00+00:00"
    # Progress is still computed from the calendar window — that's the whole
    # point of returning the empty window instead of None.
    assert payload["elapsed_pct"] == pytest.approx(28.6, rel=0.01)
    assert payload["remaining_human"] != ""
