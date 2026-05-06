"""Digester: weekly summary of detection / proposal / apply / measurement.

Aggregates JSONL streams under ~/.agent-doctor/<host>/ over a 7-day
window. Renders via speaker.render_digest. Posts via adapter.send_message
when called from the CLI digest command (which resolves a Target the
same way other speak paths do).

The digester is intentionally tolerant of timestamps:
- proposals.jsonl always carries `created_at` (unix seconds).
- measurements.jsonl always carries `measured_at`.
- events.jsonl from autopilot today does NOT carry a top-level `ts`
  field. When a row has no recognized timestamp, it is included
  unconditionally (counted as in-window). This is acceptable for v1
  because users bootstrap a fresh ~/.agent-doctor/<host>/ at install
  time; once timestamps are added to events.jsonl the filter will
  kick in automatically without code changes.
"""
from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional


@dataclass(frozen=True)
class WeeklyDigest:
    """Aggregated counts over a 7-day window for one host."""

    host: str
    events: int = 0
    proposed: int = 0
    applied: int = 0
    dismissed: int = 0
    measured_better: int = 0
    top_patterns: tuple[str, ...] = ()
    since_ts: float = 0.0
    until_ts: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def build_weekly_digest(
    host: str,
    *,
    since_ts: Optional[float] = None,
    until_ts: Optional[float] = None,
) -> WeeklyDigest:
    """Aggregate ~/.agent-doctor/<host>/{events,proposals,measurements}.jsonl.

    Window defaults to the last 7 days. Rows without any recognized
    timestamp field are included (autopilot events.jsonl rows have no
    `ts` today; see module docstring).
    """
    now = time.time()
    until = until_ts if until_ts is not None else now
    since = since_ts if since_ts is not None else (now - 7 * 86400)

    base = Path("~/.agent-doctor").expanduser() / host

    triggers: Counter[str] = Counter()
    events_count = 0
    for row in _iter_jsonl(base / "events.jsonl"):
        ts = _row_timestamp(row, default_keys=("ts", "created_at", "at"))
        if ts is not None and not (since <= ts <= until):
            continue
        events_count += 1
        trigger = str(row.get("trigger", "") or "")
        if trigger:
            triggers[trigger] += 1

    proposed = applied = dismissed = 0
    for row in _iter_jsonl(base / "proposals.jsonl"):
        ts = _row_timestamp(row, default_keys=("created_at",))
        if ts is not None and not (since <= ts <= until):
            continue
        proposed += 1
        state = str(row.get("state", "") or "")
        if state == "applied":
            applied += 1
        elif state == "dismissed":
            dismissed += 1

    measured_better = 0
    for row in _iter_jsonl(base / "measurements.jsonl"):
        ts = _row_timestamp(row, default_keys=("measured_at",))
        if ts is not None and not (since <= ts <= until):
            continue
        score = row.get("score")
        try:
            if score is not None and float(score) >= 0.7:
                measured_better += 1
        except (TypeError, ValueError):
            continue

    top = tuple(t for t, _ in triggers.most_common(5))

    return WeeklyDigest(
        host=host,
        events=events_count,
        proposed=proposed,
        applied=applied,
        dismissed=dismissed,
        measured_better=measured_better,
        top_patterns=top,
        since_ts=since,
        until_ts=until,
    )


def _iter_jsonl(path: Path) -> Iterator[dict]:
    """Yield one decoded dict per non-blank, well-formed line. Skips bad lines."""
    if not path.exists():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            yield row


def _row_timestamp(row: dict, *, default_keys: tuple[str, ...]) -> float | None:
    """Return the first parseable timestamp from `default_keys`, else None."""
    for key in default_keys:
        v = row.get(key)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None
