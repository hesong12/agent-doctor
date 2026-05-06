"""Tier 2 classifier: host-inference second pass for borderline cases.

Calls adapter.infer_text with a few-shot prompt; parses JSON response.
Per-(message_hash, model) cache + per-day call cap in SQLite.

Tier 2 is OPT-IN by capability: if adapter.capabilities().can_infer_text
is False, returns skipped=True without making a call.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Tier2Result:
    severity: str  # "none" | "low" | "medium" | "high"
    signals: tuple[str, ...] = ()
    rationale: str = ""
    cached: bool = False
    skipped: bool = False


_PROMPT_TEMPLATE = """Classify whether this user message in an AI-agent conversation expresses frustration.
Reply with strict JSON only: {{"severity": "none|low|medium|high", "signals": [...], "rationale": "..."}}.

Examples:
- "actually nm I'll figure it out myself" -> {{"severity": "high", "signals": ["trust_break", "give_up"], "rationale": "user giving up on agent"}}
- "interesting choice" (sarcastic context) -> {{"severity": "medium", "signals": ["sarcasm"], "rationale": "sarcastic disapproval"}}
- "为什么我说了三遍你还在做错的事情" -> {{"severity": "high", "signals": ["repeated_correction"], "rationale": "explicit count of repeated corrections"}}
- "thanks!" -> {{"severity": "none", "signals": [], "rationale": "positive"}}

Now classify:
{message}"""


_STRICT_RETRY_PROMPT = """Reply ONLY with valid JSON in this exact form, nothing else:
{{"severity": "none|low|medium|high", "signals": ["..."], "rationale": "..."}}

Classify the user message:
{message}"""


def tier2_classify(
    text: str,
    adapter,
    *,
    cache_path: Optional[Path] = None,
    model: Optional[str] = None,
    max_calls_per_day: int = 100,
) -> Tier2Result:
    """Run Tier 2 host-inference classification.

    Returns Tier2Result with skipped=True if the adapter cannot infer,
    the daily cap is reached, the adapter raises, or JSON parsing fails
    twice in a row. Otherwise returns the parsed severity / signals /
    rationale, with cached=True if served from the SQLite cache.
    """

    caps = adapter.capabilities()
    if not caps.can_infer_text:
        return Tier2Result(
            severity="none",
            skipped=True,
            rationale="adapter has no infer capability",
        )

    msg_hash = _hash(text)
    model_key = model or "default"

    conn = _open_cache(cache_path) if cache_path else None
    try:
        if conn is not None:
            cached = _read_cache(conn, msg_hash, model_key)
            if cached is not None:
                return Tier2Result(
                    severity=cached["severity"],
                    signals=tuple(cached["signals"]),
                    rationale=cached["rationale"],
                    cached=True,
                )
            if not _under_cap(conn, max_calls_per_day):
                return Tier2Result(
                    severity="none",
                    skipped=True,
                    rationale="daily cap reached",
                )

        # First attempt with the few-shot prompt.
        try:
            response = adapter.infer_text(
                _PROMPT_TEMPLATE.format(message=text), model=model
            )
        except (RuntimeError, OSError, NotImplementedError):
            return Tier2Result(
                severity="none",
                skipped=True,
                rationale="adapter error",
            )

        parsed = _parse_json(response)
        if parsed is None:
            # Retry once with a stricter prompt before giving up.
            try:
                response = adapter.infer_text(
                    _STRICT_RETRY_PROMPT.format(message=text), model=model
                )
            except (RuntimeError, OSError, NotImplementedError):
                return Tier2Result(
                    severity="none",
                    skipped=True,
                    rationale="adapter error on retry",
                )
            parsed = _parse_json(response)

        if parsed is None:
            return Tier2Result(
                severity="none",
                skipped=True,
                rationale="parse failure after retry",
            )

        if conn is not None:
            _write_cache(conn, msg_hash, model_key, parsed)
            _bump_call_count(conn)

        return Tier2Result(
            severity=parsed["severity"],
            signals=tuple(parsed["signals"]),
            rationale=parsed["rationale"],
        )
    finally:
        if conn is not None:
            conn.close()


# --- helpers ----------------------------------------------------------------


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_json(response: str) -> dict | None:
    """Permissive JSON extraction: tries direct parse, then a {...} block strip."""
    if not isinstance(response, str):
        return None
    try:
        d = json.loads(response.strip())
    except json.JSONDecodeError:
        stripped = response.strip()
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            try:
                d = json.loads(stripped[start:end + 1])
            except json.JSONDecodeError:
                return None
        else:
            return None
    if not isinstance(d, dict):
        return None
    if "severity" not in d or d["severity"] not in ("none", "low", "medium", "high"):
        return None
    signals = d.get("signals", [])
    if not isinstance(signals, list):
        signals = []
    rationale = str(d.get("rationale", ""))
    return {
        "severity": d["severity"],
        "signals": [str(s) for s in signals],
        "rationale": rationale,
    }


def _open_cache(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tier2_cache (
            message_hash TEXT NOT NULL,
            model TEXT NOT NULL,
            severity TEXT,
            signals TEXT,
            rationale TEXT,
            cached_at REAL,
            PRIMARY KEY (message_hash, model)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tier2_calls (
            day TEXT PRIMARY KEY,
            count INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()
    return conn


def _read_cache(conn: sqlite3.Connection, msg_hash: str, model: str) -> dict | None:
    row = conn.execute(
        "SELECT severity, signals, rationale FROM tier2_cache WHERE message_hash=? AND model=?",
        (msg_hash, model),
    ).fetchone()
    if row is None:
        return None
    severity, signals_json, rationale = row
    try:
        signals = json.loads(signals_json) if signals_json else []
    except json.JSONDecodeError:
        signals = []
    return {
        "severity": severity,
        "signals": signals,
        "rationale": rationale or "",
    }


def _write_cache(
    conn: sqlite3.Connection, msg_hash: str, model: str, parsed: dict
) -> None:
    import time as _time

    conn.execute(
        """INSERT OR REPLACE INTO tier2_cache(
            message_hash, model, severity, signals, rationale, cached_at
        ) VALUES (?, ?, ?, ?, ?, ?)""",
        (
            msg_hash,
            model,
            parsed["severity"],
            json.dumps(parsed["signals"]),
            parsed["rationale"],
            _time.time(),
        ),
    )
    conn.commit()


def _today_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


def _under_cap(conn: sqlite3.Connection, cap: int) -> bool:
    today = _today_utc()
    row = conn.execute(
        "SELECT count FROM tier2_calls WHERE day=?", (today,)
    ).fetchone()
    count = row[0] if row else 0
    return count < cap


def _bump_call_count(conn: sqlite3.Connection) -> None:
    today = _today_utc()
    conn.execute(
        """INSERT INTO tier2_calls(day, count) VALUES (?, 1)
           ON CONFLICT(day) DO UPDATE SET count = count + 1""",
        (today,),
    )
    conn.commit()
