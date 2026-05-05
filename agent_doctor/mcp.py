"""Model Context Protocol server for Agent Doctor.

Exposes the same diagnosis surface as the CLI through MCP so any MCP-aware
host (Claude Desktop, Cursor, Cline, Continue, Hermes, OpenClaw …) can call
agent-doctor mid-session instead of asking the user to drop to a shell.

Trust boundary
--------------

The MCP server inherits the same local-first contract as every other entry
point. Specifically:

* Read tools (``scan``, ``list_findings``, ``read_finding``, ``bench``) only
  read from disk and write artifacts into directories the caller chose.
* Write tools (``stage_patches``, ``generate_corpus``) only write under
  ``staging_dir`` / ``out_dir``. They never touch live host-agent
  configuration. Tool descriptions advertise this contract explicitly so an
  agent reasoning about which tool to call sees the constraint.
* No tool calls a remote LLM. ``generate_corpus`` deliberately omits the
  ``--llm`` switch: LLM-augmented generation stays a CLI-only path so the
  default MCP surface is fully offline.

Module layout
-------------

The pure-Python tool handlers (``tool_scan``, ``tool_list_findings``, …) do
not import the MCP SDK. They take a JSON-shaped ``arguments`` dict, do work,
and return a JSON string. Tests exercise them directly.

The SDK glue lives in :func:`serve` and :func:`build_server`, which lazy-
import ``mcp`` so the rest of agent-doctor works without the optional extra.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .apply import load_findings, stage_patches
from .detectors import detect_findings
from .ingest import (
    DEFAULT_HERMES_PATH,
    DEFAULT_OPENCLAW_PATH,
    IngestError,
    ingest_path_with_errors,
)
from .report import write_reports


SERVER_NAME = "agent-doctor"
SERVER_VERSION = "0.2.0"


@dataclass(frozen=True)
class ToolDefinition:
    """One MCP tool, decoupled from the SDK so tests can inspect the schema."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], str]


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


def tool_scan(arguments: dict[str, Any]) -> str:
    """Scan a JSONL transcript directory or file and write report artifacts.

    Required: ``out_dir``. Either ``path`` or ``host`` must be provided.
    Returns a JSON envelope with summary stats and the full findings list so
    a caller has everything inline without a follow-up read.
    """

    out_dir = _require_path(arguments, "out_dir")
    path = arguments.get("path")
    host = arguments.get("host")
    strict = bool(arguments.get("strict", False))

    if path and host:
        return _error("provide either `path` or `host`, not both")
    if not path and not host:
        return _error("provide `path` or `host`")

    if host == "hermes":
        target = DEFAULT_HERMES_PATH
    elif host == "openclaw":
        target = DEFAULT_OPENCLAW_PATH
    elif host:
        return _error(f"unknown host {host!r}; expected hermes or openclaw")
    else:
        target = Path(path).expanduser()

    try:
        messages, parse_errors = ingest_path_with_errors(target, strict=strict)
    except IngestError as exc:
        return _error(str(exc))

    findings = detect_findings(messages)
    output_paths = write_reports(out_dir, messages, findings, parse_errors=parse_errors)

    return _ok(
        {
            "scan_dir": str(out_dir),
            "summary": {
                "messages": len(messages),
                "sessions": len({message.session_id for message in messages}),
                "findings": len(findings),
                "parse_errors": parse_errors,
            },
            "artifacts": {key: str(value) for key, value in output_paths.items()},
            "findings": [_finding_summary(finding.to_dict()) for finding in findings],
        }
    )


def tool_list_findings(arguments: dict[str, Any]) -> str:
    """Return finding summaries from a previous ``scan`` output directory."""

    scan_dir = _require_path(arguments, "scan_dir")
    findings_path = scan_dir / "findings.json"
    if not findings_path.exists():
        return _error(f"findings.json not found in {scan_dir}")
    findings = load_findings(findings_path)
    return _ok(
        {
            "scan_dir": str(scan_dir),
            "count": len(findings),
            "findings": [_finding_summary(finding) for finding in findings],
        }
    )


def tool_read_finding(arguments: dict[str, Any]) -> str:
    """Return a single finding (with full evidence) by id."""

    scan_dir = _require_path(arguments, "scan_dir")
    finding_id = arguments.get("finding_id")
    if not finding_id:
        return _error("`finding_id` is required")
    findings_path = scan_dir / "findings.json"
    if not findings_path.exists():
        return _error(f"findings.json not found in {scan_dir}")
    findings = load_findings(findings_path)
    for finding in findings:
        if finding.get("id") == finding_id:
            return _ok({"finding": finding})
    return _error(f"no finding with id {finding_id!r} in {findings_path}")


def tool_bench(arguments: dict[str, Any]) -> str:
    """Run the detection benchmark against a corpus produced by ``generate_corpus``."""

    corpus_dir = _require_path(arguments, "corpus_dir")
    out_dir = _require_path(arguments, "out_dir")
    from .evals.bench import run_benchmark

    try:
        result = run_benchmark(corpus_dir, out_dir)
    except FileNotFoundError as exc:
        return _error(str(exc))
    return _ok({"out_dir": str(out_dir), "summary": result.to_summary()})


# ---------------------------------------------------------------------------
# Write tools (staging only)
# ---------------------------------------------------------------------------


def tool_stage_patches(arguments: dict[str, Any]) -> str:
    """Stage reviewable patches into ``staging_dir``.

    Never modifies live host-agent configuration. The only filesystem writes
    are under ``staging_dir`` (and a unified diff vs. ``target_dir`` if the
    caller supplied one — read-only against the target, write-only into
    staging).
    """

    findings_path = _require_path(arguments, "findings_path")
    if findings_path.is_dir():
        findings_path = findings_path / "findings.json"
    staging_dir = _require_path(arguments, "staging_dir")
    target_dir_str = arguments.get("target_dir")
    target_dir = Path(target_dir_str).expanduser() if target_dir_str else None
    minimum_severity = arguments.get("min_severity", "low")
    if minimum_severity not in {"low", "medium", "high"}:
        return _error("`min_severity` must be low, medium, or high")
    minimum_count = int(arguments.get("min_count", 1))

    if not findings_path.exists():
        return _error(f"findings file not found: {findings_path}")
    findings = load_findings(findings_path)
    result = stage_patches(
        findings,
        staging_dir,
        target_dir=target_dir,
        minimum_severity=minimum_severity,
        minimum_count=minimum_count,
    )
    return _ok(
        {
            "staging_dir": str(result.staging_dir),
            "files_written": [str(path) for path in result.files_written],
            "skipped": result.skipped,
            "diff_present": bool(result.diff_text),
        }
    )


def tool_generate_corpus(arguments: dict[str, Any]) -> str:
    """Generate a synthetic eval corpus from scenario cards.

    Template-based only. The optional LLM-backed generator is a CLI-only path
    so the MCP surface stays fully offline by default.
    """

    cards_dir = _require_path(arguments, "cards_dir")
    out_dir = _require_path(arguments, "out_dir")
    seed = int(arguments.get("seed", 0))

    from .evals.generator import generate_corpus

    summary = generate_corpus(cards_dir, out_dir, use_llm=False, seed=seed)
    return _ok({"out_dir": str(out_dir), "summary": summary})


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


TOOL_DEFINITIONS: list[ToolDefinition] = [
    ToolDefinition(
        name="scan",
        description=(
            "Scan a JSONL transcript directory and write a report. Local-only; "
            "no network calls. Pass `path` or `host` ('hermes'/'openclaw'). "
            "Writes report.md, findings.json, and eval-cases.yaml under `out_dir`."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "out_dir": {"type": "string", "description": "Where to write report artifacts."},
                "path": {"type": "string", "description": "JSONL file or directory of JSONL files."},
                "host": {
                    "type": "string",
                    "enum": ["hermes", "openclaw"],
                    "description": "Use this host's default sessions path instead of `path`.",
                },
                "strict": {"type": "boolean", "default": False},
            },
            "required": ["out_dir"],
            "additionalProperties": False,
        },
        handler=tool_scan,
    ),
    ToolDefinition(
        name="list_findings",
        description="List finding summaries (id, severity, count, mode, title) from a previous scan_dir.",
        input_schema={
            "type": "object",
            "properties": {"scan_dir": {"type": "string"}},
            "required": ["scan_dir"],
            "additionalProperties": False,
        },
        handler=tool_list_findings,
    ),
    ToolDefinition(
        name="read_finding",
        description="Return a single finding (with full evidence) by id from a scan_dir.",
        input_schema={
            "type": "object",
            "properties": {
                "scan_dir": {"type": "string"},
                "finding_id": {"type": "string"},
            },
            "required": ["scan_dir", "finding_id"],
            "additionalProperties": False,
        },
        handler=tool_read_finding,
    ),
    ToolDefinition(
        name="bench",
        description=(
            "Run the deterministic detection benchmark against a labeled corpus. "
            "Writes bench.json and bench.md under `out_dir`. No network calls."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "corpus_dir": {"type": "string"},
                "out_dir": {"type": "string"},
            },
            "required": ["corpus_dir", "out_dir"],
            "additionalProperties": False,
        },
        handler=tool_bench,
    ),
    ToolDefinition(
        name="stage_patches",
        description=(
            "Stage reviewable patches from findings.json into `staging_dir`. "
            "NEVER modifies live host-agent configuration; the only filesystem "
            "writes happen under `staging_dir`. If `target_dir` is given, the "
            "tool reads from it (read-only) to produce a unified diff."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "findings_path": {
                    "type": "string",
                    "description": "Path to a findings.json or to a scan output directory.",
                },
                "staging_dir": {"type": "string"},
                "target_dir": {"type": "string"},
                "min_severity": {"type": "string", "enum": ["low", "medium", "high"], "default": "low"},
                "min_count": {"type": "integer", "default": 1, "minimum": 1},
            },
            "required": ["findings_path", "staging_dir"],
            "additionalProperties": False,
        },
        handler=tool_stage_patches,
    ),
    ToolDefinition(
        name="generate_corpus",
        description=(
            "Generate a synthetic eval corpus (transcripts + ground truth labels) "
            "from scenario cards. Template-based; no network calls."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "cards_dir": {"type": "string"},
                "out_dir": {"type": "string"},
                "seed": {"type": "integer", "default": 0},
            },
            "required": ["cards_dir", "out_dir"],
            "additionalProperties": False,
        },
        handler=tool_generate_corpus,
    ),
]


def find_tool(name: str) -> ToolDefinition | None:
    for tool in TOOL_DEFINITIONS:
        if tool.name == name:
            return tool
    return None


# ---------------------------------------------------------------------------
# JSON envelope helpers
# ---------------------------------------------------------------------------


def _ok(payload: dict[str, Any]) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False, indent=2)


def _error(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False, indent=2)


def _require_path(arguments: dict[str, Any], key: str) -> Path:
    value = arguments.get(key)
    if not value:
        raise ValueError(f"`{key}` is required")
    return Path(value).expanduser()


def _finding_summary(finding: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": finding.get("id"),
        "severity": finding.get("severity"),
        "failure_mode": finding.get("failure_mode"),
        "title": finding.get("title"),
        "session_id": finding.get("session_id"),
        "count": finding.get("count", 1),
        "evidence_count": len(finding.get("evidence", []) or []),
    }


# ---------------------------------------------------------------------------
# CLI helpers (kept for backward compatibility with `agent-doctor mcp`)
# ---------------------------------------------------------------------------


def placeholder_payload() -> dict[str, Any]:
    """Return server metadata for ``agent-doctor mcp`` (no-arg form).

    Historically this returned ``status: placeholder``. Now that the real
    server exists it advertises the live tool list and a config snippet so a
    user running ``agent-doctor mcp`` immediately sees what's available.
    """

    return {
        "name": SERVER_NAME,
        "version": SERVER_VERSION,
        "status": "ready",
        "transport": "stdio",
        "privacy": "local-only; no network calls",
        "command": "agent-doctor mcp serve",
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "writes": tool.name in {"scan", "stage_patches", "generate_corpus", "bench"},
            }
            for tool in TOOL_DEFINITIONS
        ],
        "install_extra": "pip install 'agent-doctor[mcp]'",
    }


# ---------------------------------------------------------------------------
# SDK glue (lazy-imported)
# ---------------------------------------------------------------------------


def _missing_sdk_message() -> str:
    return (
        "MCP server requires the `mcp` extra. Install with:\n"
        "  pip install 'agent-doctor[mcp]'\n"
        "or, when installing from GitHub:\n"
        "  pip install 'agent-doctor[mcp] @ git+https://github.com/USER/agent-doctor.git'"
    )


def build_server() -> Any:
    """Build and return an MCP ``Server`` with all tool handlers registered.

    Lazy-imports the ``mcp`` SDK so the rest of agent-doctor still imports
    cleanly without the optional extra. Raises ``RuntimeError`` with a
    user-actionable install hint when the SDK is missing.
    """

    try:
        from mcp.server import Server
        import mcp.types as mcp_types
    except ImportError as exc:
        raise RuntimeError(_missing_sdk_message()) from exc

    server: Any = Server(SERVER_NAME)

    @server.list_tools()
    async def _list_tools() -> list[Any]:
        return [
            mcp_types.Tool(
                name=tool.name,
                description=tool.description,
                inputSchema=tool.input_schema,
            )
            for tool in TOOL_DEFINITIONS
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> list[Any]:
        tool = find_tool(name)
        if tool is None:
            return [mcp_types.TextContent(type="text", text=_error(f"unknown tool: {name}"))]
        try:
            text = tool.handler(arguments or {})
        except Exception as exc:  # pragma: no cover — surface unexpected errors as JSON
            text = _error(f"{type(exc).__name__}: {exc}")
        return [mcp_types.TextContent(type="text", text=text)]

    return server


def serve() -> None:
    """Run the MCP stdio server. Lazy-imports the SDK."""

    try:
        import asyncio

        from mcp.server.stdio import stdio_server
    except ImportError as exc:
        raise RuntimeError(_missing_sdk_message()) from exc

    server = build_server()

    async def _main() -> None:
        async with stdio_server() as (read_stream, write_stream):
            init_options = server.create_initialization_options()
            await server.run(read_stream, write_stream, init_options)

    asyncio.run(_main())


def main() -> None:
    """Entry point for ``python -m agent_doctor.mcp``."""

    print(json.dumps(placeholder_payload(), indent=2))


if __name__ == "__main__":
    main()
