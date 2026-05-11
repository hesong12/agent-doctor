"""Claude + Codex usage collection for the desktop pet popover.

This module shells out to the published ``ccusage`` (Claude) and
``@ccusage/codex`` (Codex) ``npx`` packages. Both are read-only — no live
quota API and no Anthropic / OpenAI credentials are read. The packages
parse the local agent transcripts the user already has on disk.

Public surface
--------------

:func:`collect_usage` runs all four queries in parallel with a per-call
timeout and returns a JSON-serializable dict::

    {
      "claude": {
        "window_5h":     {tokens, cost_usd, models, start_iso, end_iso,
                          reset_minutes, elapsed_pct, remaining_minutes,
                          remaining_human} | None,
        "window_weekly": {tokens, cost_usd, models, start_iso, end_iso,
                          elapsed_pct, remaining_minutes, remaining_human} | None,
        "error": str | None,
      },
      "codex":  { ... },
      "generated_at": "2026-05-10T20:00:00+00:00",
    }

Failure handling
----------------

The collector **must never raise to the caller**. The popover in the
desktop pet relies on getting *some* JSON back so it can render an
install-hint card even when ``npx``/the packages are missing. Each per-
source failure (binary not on PATH, JSON unparseable, subprocess
timeout, empty blocks) populates the parent ``error`` field with a
human-readable message and the corresponding ``window_*`` fields stay
``None``. Other windows still serialize normally.
"""

from __future__ import annotations

import concurrent.futures
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 15.0
"""Per-call timeout. Each of the four npx queries is bounded independently."""

_FIVE_HOURS_SECONDS = 5 * 60 * 60
_WEEK_DAYS = 7

CCUSAGE_PACKAGE = "ccusage@latest"
CODEX_PACKAGE = "@ccusage/codex@latest"


# ---------------------------------------------------------------------------
#  Public dataclasses / API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """A single usage window (5h rolling or weekly)."""

    tokens_input: int
    tokens_output: int
    tokens_cache: int
    tokens_total: int
    cost_usd: float
    models: tuple[str, ...]
    start_iso: str
    end_iso: str
    reset_minutes: int | None = None

    def to_dict(self, *, now_epoch: float | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tokens": {
                "input": self.tokens_input,
                "output": self.tokens_output,
                "cache": self.tokens_cache,
                "total": self.tokens_total,
            },
            "cost_usd": round(self.cost_usd, 6),
            "models": list(self.models),
            "start_iso": self.start_iso,
            "end_iso": self.end_iso,
        }
        if self.reset_minutes is not None:
            payload["reset_minutes"] = int(self.reset_minutes)
        elapsed_pct, remaining_minutes, remaining_human = _compute_progress(
            self.start_iso,
            self.end_iso,
            now_epoch=now_epoch if now_epoch is not None else _now_epoch(),
            reset_minutes_override=self.reset_minutes,
        )
        payload["elapsed_pct"] = elapsed_pct
        payload["remaining_minutes"] = remaining_minutes
        payload["remaining_human"] = remaining_human
        return payload


def collect_usage(*, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Run the four npx queries in parallel and assemble the payload.

    Each query is bounded by ``timeout`` independently. The collector
    swallows every exception so a single bad source never poisons the
    other three. The ``generated_at`` field is always set so popovers
    can show "data last refreshed at..." even on full failure.
    """

    generated_at = _now_iso()
    # Snapshot "now" once so all four window-progress calculations agree on
    # the same reference instant — drift between four ``_now_epoch()`` calls
    # is microseconds, but the unit tests monkey-patch ``_now_epoch`` and
    # expect a single deterministic value across the payload.
    now_epoch_snapshot = _now_epoch()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            "claude_5h": pool.submit(_safe_call, _claude_5h, timeout),
            "claude_wk": pool.submit(_safe_call, _claude_weekly, timeout),
            "codex_5h": pool.submit(_safe_call, _codex_5h, timeout),
            "codex_wk": pool.submit(_safe_call, _codex_weekly, timeout),
        }
        results = {name: fut.result() for name, fut in futures.items()}

    claude_5h_value, claude_5h_err = results["claude_5h"]
    claude_wk_value, claude_wk_err = results["claude_wk"]
    codex_5h_value, codex_5h_err = results["codex_5h"]
    codex_wk_value, codex_wk_err = results["codex_wk"]

    def _serialize(window: Window | None) -> dict[str, Any] | None:
        return window.to_dict(now_epoch=now_epoch_snapshot) if window else None

    return {
        "claude": {
            "window_5h": _serialize(claude_5h_value),
            "window_weekly": _serialize(claude_wk_value),
            "error": _combine_errors(claude_5h_err, claude_wk_err, source="ccusage"),
        },
        "codex": {
            "window_5h": _serialize(codex_5h_value),
            "window_weekly": _serialize(codex_wk_value),
            "error": _combine_errors(
                codex_5h_err, codex_wk_err, source="@ccusage/codex"
            ),
        },
        "generated_at": generated_at,
    }


# ---------------------------------------------------------------------------
#  npx invocation helpers
# ---------------------------------------------------------------------------


def _safe_call(
    fn: Any, timeout: float
) -> tuple[Window | None, str | None]:
    """Run ``fn(timeout)``, never raise. Returns (window, error_message)."""

    try:
        return fn(timeout), None
    except UsageError as exc:
        return None, str(exc)
    except Exception as exc:  # pragma: no cover — last-resort net
        return None, f"unexpected error: {exc}"


class UsageError(RuntimeError):
    """Human-readable failure from one of the npx sources."""


def _run_npx_json(args: list[str], timeout: float) -> Any:
    """Run ``npx <args>`` and parse stdout as JSON, raising :class:`UsageError`."""

    if shutil.which("npx") is None:
        raise UsageError(
            "npx is not on PATH — install Node.js to enable usage stats."
        )
    cmd = ["npx", *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise UsageError(
            f"`{' '.join(cmd)}` timed out after {timeout:.0f}s"
        ) from exc
    except OSError as exc:
        raise UsageError(f"could not execute `{' '.join(cmd)}`: {exc}") from exc

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip().splitlines()
        tail = stderr[-1] if stderr else f"exit {proc.returncode}"
        raise UsageError(f"`{' '.join(cmd)}` failed: {tail}")

    raw = (proc.stdout or "").strip()
    if not raw:
        raise UsageError(f"`{' '.join(cmd)}` produced no output")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UsageError(
            f"`{' '.join(cmd)}` returned unparseable JSON: {exc.msg}"
        ) from exc


# ---------------------------------------------------------------------------
#  Claude — ccusage
# ---------------------------------------------------------------------------


def _claude_5h(timeout: float) -> Window:
    payload = _run_npx_json(
        [CCUSAGE_PACKAGE, "blocks", "--active", "--json", "--offline"],
        timeout=timeout,
    )
    blocks = _list_field(payload, "blocks")
    active = _pick_active_block(blocks)
    if active is None:
        raise UsageError("no active 5h block (nothing used in the last 5h)")
    return _claude_block_to_window(active)


def _pick_active_block(blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if bool(block.get("isActive")):
            return block
    return None


def _claude_block_to_window(block: dict[str, Any]) -> Window:
    counts = block.get("tokenCounts") or {}
    input_tokens = _coerce_int(
        counts.get("inputTokens") or counts.get("input") or 0
    )
    output_tokens = _coerce_int(
        counts.get("outputTokens") or counts.get("output") or 0
    )
    cache_tokens = _coerce_int(
        counts.get("cacheCreationInputTokens")
        or counts.get("cacheCreationTokens")
        or counts.get("cacheCreation")
        or counts.get("cache")
        or 0
    ) + _coerce_int(
        counts.get("cacheReadInputTokens")
        or counts.get("cacheReadTokens")
        or counts.get("cacheRead")
        or 0
    )
    total = _coerce_int(
        block.get("totalTokens")
        or counts.get("totalTokens")
        or input_tokens + output_tokens
    )
    cost = _coerce_float(block.get("costUSD") or block.get("totalCost") or 0.0)
    models = _coerce_str_tuple(block.get("models"))
    start_iso = _normalize_iso(block.get("startTime") or block.get("startsAt") or "")
    end_iso = _normalize_iso(block.get("endTime") or block.get("endsAt") or "")
    reset = block.get("projection") or {}
    reset_minutes_value: Any = reset.get("remainingMinutes") if isinstance(reset, dict) else None
    reset_minutes = _coerce_int(reset_minutes_value) if reset_minutes_value is not None else None
    return Window(
        tokens_input=input_tokens,
        tokens_output=output_tokens,
        tokens_cache=cache_tokens,
        tokens_total=total,
        cost_usd=cost,
        models=models,
        start_iso=start_iso,
        end_iso=end_iso,
        reset_minutes=reset_minutes,
    )


def _claude_weekly(timeout: float) -> Window:
    payload = _run_npx_json(
        [CCUSAGE_PACKAGE, "weekly", "--json", "--offline"],
        timeout=timeout,
    )
    weekly = _list_field(payload, "weekly")
    if not weekly:
        raise UsageError("ccusage returned an empty weekly list")
    latest = _latest_weekly_entry(weekly)
    return _claude_weekly_to_window(latest)


def _latest_weekly_entry(entries: list[dict[str, Any]]) -> dict[str, Any]:
    sortable = [e for e in entries if isinstance(e, dict) and e.get("week")]
    if not sortable:
        return entries[-1] if entries else {}
    sortable.sort(key=lambda e: str(e.get("week")))
    return sortable[-1]


def _claude_weekly_to_window(entry: dict[str, Any]) -> Window:
    total = _coerce_int(entry.get("totalTokens") or 0)
    cost = _coerce_float(entry.get("totalCost") or entry.get("totalCostUSD") or 0.0)
    models = _coerce_str_tuple(entry.get("modelsUsed") or entry.get("models"))

    input_tokens = 0
    output_tokens = 0
    cache_tokens = 0
    breakdowns = entry.get("modelBreakdowns") or []
    if isinstance(breakdowns, list):
        for item in breakdowns:
            if not isinstance(item, dict):
                continue
            input_tokens += _coerce_int(
                item.get("inputTokens") or item.get("input") or 0
            )
            output_tokens += _coerce_int(
                item.get("outputTokens") or item.get("output") or 0
            )
            cache_tokens += _coerce_int(
                item.get("cacheCreationInputTokens")
                or item.get("cacheCreationTokens")
                or 0
            ) + _coerce_int(
                item.get("cacheReadInputTokens")
                or item.get("cacheReadTokens")
                or 0
            )
    if total == 0:
        total = input_tokens + output_tokens

    start_iso, end_iso = _week_window_iso(entry.get("week"))
    return Window(
        tokens_input=input_tokens,
        tokens_output=output_tokens,
        tokens_cache=cache_tokens,
        tokens_total=total,
        cost_usd=cost,
        models=models,
        start_iso=start_iso,
        end_iso=end_iso,
    )


def _week_window_iso(week: Any) -> tuple[str, str]:
    """Best-effort ISO bounds for a ``YYYY-MM-DD`` (week-start) value."""

    if isinstance(week, str) and week:
        try:
            start = datetime.fromisoformat(_iso_for_fromisoformat(week))
        except ValueError:
            start = None
        if start is not None:
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            end = start + timedelta(days=_WEEK_DAYS)
            return start.isoformat(), end.isoformat()
    now = datetime.now(timezone.utc)
    return (now - timedelta(days=_WEEK_DAYS)).isoformat(), now.isoformat()


# ---------------------------------------------------------------------------
#  Codex — @ccusage/codex (no blocks / weekly subcommands → synthesize)
# ---------------------------------------------------------------------------


def _codex_5h(timeout: float) -> Window:
    payload = _run_npx_json(
        [CODEX_PACKAGE, "session", "--json", "--offline"],
        timeout=timeout,
    )
    sessions = _codex_session_list(payload)
    cutoff = _now_epoch() - _FIVE_HOURS_SECONDS
    recent = [s for s in sessions if _codex_session_in_window(s, cutoff_epoch=cutoff)]
    if not recent:
        raise UsageError("no Codex sessions in the last 5h")
    return _aggregate_codex_entries(
        recent,
        start_iso=_iso_at(cutoff),
        end_iso=_now_iso(),
    )


def _codex_session_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [s for s in payload if isinstance(s, dict)]
    if isinstance(payload, dict):
        for key in ("sessions", "session", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [s for s in value if isinstance(s, dict)]
    return []


def _codex_session_in_window(session: dict[str, Any], *, cutoff_epoch: float) -> bool:
    last = (
        session.get("lastActivity")
        or session.get("last_activity")
        or session.get("endTime")
        or session.get("lastUsed")
    )
    epoch = _parse_iso_to_epoch(last)
    if epoch is None:
        return False
    # Strict ``>`` (not ``>=``): a session that touched the cutoff to the
    # second has effectively rolled out of the active window already, so we
    # exclude it. This matches the desktop-pet contract's "5h rolling" intent
    # and keeps numbers from briefly ghosting after the window flips.
    return epoch > cutoff_epoch


def _codex_weekly(timeout: float) -> Window:
    payload = _run_npx_json(
        [CODEX_PACKAGE, "daily", "--json", "--offline"],
        timeout=timeout,
    )
    daily = _codex_daily_list(payload)
    now_epoch = _now_epoch()
    now_date = datetime.fromtimestamp(now_epoch, tz=timezone.utc).date()
    recent = [d for d in daily if _codex_daily_in_window(d, now_date=now_date)]
    if not recent:
        raise UsageError("no Codex usage in the last 7 days")
    # "Last 7 days" = today + the 6 days before it, so the window starts
    # at midnight UTC of (now - 6d). This is the most user-friendly
    # interpretation: a per-day report card never spans partial days.
    window_start = datetime(
        now_date.year, now_date.month, now_date.day, tzinfo=timezone.utc
    ) - timedelta(days=_WEEK_DAYS - 1)
    return _aggregate_codex_entries(
        recent,
        start_iso=window_start.isoformat(),
        end_iso=_now_iso(),
    )


def _codex_daily_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [d for d in payload if isinstance(d, dict)]
    if isinstance(payload, dict):
        for key in ("daily", "days", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [d for d in value if isinstance(d, dict)]
    return []


def _codex_daily_in_window(entry: dict[str, Any], *, now_date: Any) -> bool:
    """Include entries whose calendar date is within the last 7 days.

    The Codex daily endpoint emits per-day buckets keyed by a date
    string — partial days don't make sense here, so windowing is
    day-precise: ``now_date - entry_date < 7``.
    """

    entry_date = _parse_codex_date(
        entry.get("date") or entry.get("day") or entry.get("startTime")
    )
    if entry_date is None:
        return False
    delta_days = (now_date - entry_date).days
    return 0 <= delta_days < _WEEK_DAYS


# The Codex daily endpoint emits dates in US human format ("May 02, 2026")
# rather than ISO ("2026-05-02"). Hold the format strings here so both
# windowing and tests share one parser.
_CODEX_DATE_FORMATS = (
    "%b %d, %Y",   # "May 02, 2026" (real shape from @ccusage/codex daily)
    "%B %d, %Y",   # "May 02, 2026" with full month name, just in case
    "%Y-%m-%d",    # ISO date — defensive fallback if upstream switches
)


def _parse_codex_date(value: Any) -> Any:
    """Best-effort date parser for the Codex daily ``date`` field."""

    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    # ISO with time / zone suffix — let fromisoformat handle it via the
    # shared Z-suffix normalizer, so behavior stays identical to the
    # other ISO parsers in this module.
    if "T" in text or text.endswith("Z"):
        try:
            return datetime.fromisoformat(_iso_for_fromisoformat(text)).date()
        except ValueError:
            pass
    for fmt in _CODEX_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _aggregate_codex_entries(
    entries: list[dict[str, Any]],
    *,
    start_iso: str,
    end_iso: str,
) -> Window:
    input_tokens = 0
    output_tokens = 0
    cache_tokens = 0
    total = 0
    cost = 0.0
    models: list[str] = []
    for entry in entries:
        input_tokens += _coerce_int(
            entry.get("inputTokens")
            or entry.get("input")
            or (entry.get("tokenCounts") or {}).get("inputTokens")
            or 0
        )
        output_tokens += _coerce_int(
            entry.get("outputTokens")
            or entry.get("output")
            or (entry.get("tokenCounts") or {}).get("outputTokens")
            or 0
        )
        # The real @ccusage/codex schema uses ``cachedInputTokens`` (singular)
        # for the read-cache field and has no separate creation field, so
        # check all known names defensively. Both names contribute to the
        # cache bucket; nothing is double-counted because each key set is
        # mutually exclusive across the upstream versions we've seen.
        cache_tokens += _coerce_int(
            entry.get("cacheCreationInputTokens")
            or entry.get("cacheCreationTokens")
            or (entry.get("tokenCounts") or {}).get("cacheCreationInputTokens")
            or 0
        ) + _coerce_int(
            entry.get("cacheReadInputTokens")
            or entry.get("cacheReadTokens")
            or entry.get("cachedInputTokens")
            or (entry.get("tokenCounts") or {}).get("cacheReadInputTokens")
            or 0
        )
        total += _coerce_int(
            entry.get("totalTokens")
            or (entry.get("tokenCounts") or {}).get("totalTokens")
            or 0
        )
        # The real @ccusage/codex schema uses ``costUSD`` (camelCase, no
        # "total" prefix). Earlier prototype data used ``totalCost``, so
        # keep both for forward/backward compatibility.
        cost += _coerce_float(
            entry.get("costUSD")
            or entry.get("totalCost")
            or entry.get("totalCostUSD")
            or 0.0
        )
        for m in _codex_models(entry):
            if m and m not in models:
                models.append(m)
    if total == 0:
        total = input_tokens + output_tokens
    return Window(
        tokens_input=input_tokens,
        tokens_output=output_tokens,
        tokens_cache=cache_tokens,
        tokens_total=total,
        cost_usd=cost,
        models=tuple(models),
        start_iso=start_iso,
        end_iso=end_iso,
    )


def _codex_models(entry: dict[str, Any]) -> tuple[str, ...]:
    """Extract model names from a Codex entry.

    The live ``@ccusage/codex`` payload stores per-model breakdowns as a
    *dict* keyed by model name (``{"gpt-5.5": {...}}``), not as a list.
    Older prototype shape was a list of strings, so we accept either.
    """

    raw = entry.get("models") or entry.get("modelsUsed")
    if isinstance(raw, dict):
        return tuple(k for k in raw.keys() if isinstance(k, str) and k.strip())
    return _coerce_str_tuple(raw)


# ---------------------------------------------------------------------------
#  Coercion + time helpers
# ---------------------------------------------------------------------------


def _coerce_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def _coerce_float(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _coerce_str_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip() and item not in out:
            out.append(item)
    return tuple(out)


def _list_field(payload: Any, key: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise UsageError(f"expected JSON object, got {type(payload).__name__}")
    value = payload.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise UsageError(f"expected '{key}' to be a list")
    return [v for v in value if isinstance(v, dict)]


def _iso_for_fromisoformat(text: str) -> str:
    """Normalize an ISO-8601 string for ``datetime.fromisoformat``.

    Python 3.10 and earlier reject the ``Z`` UTC suffix, and even on
    3.11+ a few of the time-zone designators we hand off to
    ``fromisoformat`` start out as ``...Z``. Replacing the trailing
    ``Z`` with ``+00:00`` makes the value safe for every supported
    Python version *and* keeps the rest of the module consistent —
    every parse site goes through this helper rather than each caller
    re-implementing the same normalization.
    """

    stripped = text.strip()
    if stripped.endswith("Z"):
        return stripped[:-1] + "+00:00"
    return stripped


def _normalize_iso(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        return (
            datetime.fromisoformat(_iso_for_fromisoformat(value))
            .astimezone(timezone.utc)
            .isoformat()
        )
    except ValueError:
        return value


def _parse_iso_to_epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        # Already an epoch (ccusage sometimes serializes ms timestamps).
        return float(value) / 1000.0 if value > 10_000_000_000 else float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(_iso_for_fromisoformat(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _now_epoch() -> float:
    return time.time()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_at(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _format_remaining(minutes: int) -> str:
    """Render a non-negative minute count as a compact ``"Xm" / "Hh Mm" /
    "Dd Hh"`` string for the desktop pet's usage popover.

    Kept in usage.py (rather than the Swift renderer) so the popover
    layer never has to do time math — it just blits the pre-formatted
    string and the rendering matches what the CLI emits.
    """

    if minutes < 0:
        minutes = 0
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 1440:
        hours, mins = divmod(minutes, 60)
        if mins == 0:
            return f"{hours}h"
        return f"{hours}h {mins}m"
    days, remainder = divmod(minutes, 1440)
    hours = remainder // 60
    if hours == 0:
        return f"{days}d"
    return f"{days}d {hours}h"


def _compute_progress(
    start_iso: str,
    end_iso: str,
    *,
    now_epoch: float,
    reset_minutes_override: int | None = None,
) -> tuple[float, int, str]:
    """Return ``(elapsed_pct, remaining_minutes, remaining_human)`` for a
    window bounded by ``[start_iso, end_iso]``.

    ``elapsed_pct`` is the float percentage of the window already used,
    rounded to one decimal and clamped to ``[0, 100]``.
    ``remaining_minutes`` is ``max(0, round((end - now) / 60))`` unless
    ``reset_minutes_override`` is supplied — Claude 5h's ccusage payload
    carries an explicit ``projection.remainingMinutes`` that knows
    ``actualEndTime`` and is therefore more authoritative than the wall
    clock against ``endTime``.

    When ``start_iso`` or ``end_iso`` is missing / unparseable / produces a
    non-positive span, the function returns ``(0.0, 0, "")``. The empty
    ``remaining_human`` is the signal the Swift popover uses to **suppress**
    the progress row (so the user never sees ``"0% elapsed (— left)"`` for
    a window whose bounds we couldn't read).
    """

    start_epoch = _parse_iso_to_epoch(start_iso)
    end_epoch = _parse_iso_to_epoch(end_iso)
    if start_epoch is None or end_epoch is None or end_epoch <= start_epoch:
        return (0.0, 0, "")

    span = end_epoch - start_epoch
    elapsed = now_epoch - start_epoch
    pct = (elapsed / span) * 100.0
    if pct < 0.0:
        pct = 0.0
    elif pct > 100.0:
        pct = 100.0
    elapsed_pct = round(pct, 1)

    if reset_minutes_override is not None:
        remaining_minutes = max(0, int(reset_minutes_override))
    else:
        remaining_minutes = max(0, round((end_epoch - now_epoch) / 60.0))

    return (elapsed_pct, remaining_minutes, _format_remaining(remaining_minutes))


def _combine_errors(*errors: str | None, source: str) -> str | None:
    seen: list[str] = []
    for err in errors:
        if err and err not in seen:
            seen.append(err)
    if not seen:
        return None
    if len(seen) == 1:
        return f"{source}: {seen[0]}"
    return f"{source}: " + "; ".join(seen)
