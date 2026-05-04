"""Command-line interface for Agent Doctor."""

from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path

from . import __version__
from .detectors import detect_findings
from .ingest import DEFAULT_HERMES_PATH, DEFAULT_OPENCLAW_PATH, IngestError, ingest_path
from .install import install_skill
from .mcp import placeholder_payload
from .report import render_json_summary, render_summary, write_reports


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-doctor",
        description="Turn frustrating agent sessions into durable fixes.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="Scan JSONL transcript files.")
    scan.add_argument("--path", type=Path, help="JSONL file or directory containing JSONL files.")
    scan.add_argument("--hermes", action="store_true", help="Use Hermes default session path.")
    scan.add_argument("--openclaw", action="store_true", help="Use OpenClaw default session path.")
    scan.add_argument("--format", choices=["markdown", "json"], default="markdown")
    scan.add_argument("--out", type=Path, required=True, help="Output directory.")
    scan.set_defaults(func=_cmd_scan)

    doctor = subparsers.add_parser("doctor", help="Print environment and readiness info.")
    doctor.set_defaults(func=_cmd_doctor)

    install = subparsers.add_parser("install-skill", help="Generate a safe host-agent SOP file.")
    install.add_argument("--target", choices=["hermes", "openclaw"], required=True)
    install.add_argument("--out", type=Path, required=True)
    install.set_defaults(func=_cmd_install_skill)

    mcp = subparsers.add_parser("mcp", help="Print minimal MCP placeholder metadata.")
    mcp.set_defaults(func=_cmd_mcp)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except (IngestError, ValueError) as exc:
        parser.exit(2, f"agent-doctor: error: {exc}\n")


def _cmd_scan(args: argparse.Namespace) -> int:
    selected_defaults = [flag for flag in (args.hermes, args.openclaw) if flag]
    if args.path and selected_defaults:
        raise ValueError("Use either --path or a default path flag, not both.")
    if len(selected_defaults) > 1:
        raise ValueError("Use only one default path flag.")

    input_path = args.path
    if args.hermes:
        input_path = DEFAULT_HERMES_PATH
    elif args.openclaw:
        input_path = DEFAULT_OPENCLAW_PATH
    if input_path is None:
        raise ValueError("Provide --path, --hermes, or --openclaw.")

    messages = ingest_path(input_path)
    findings = detect_findings(messages)
    output_paths = write_reports(args.out, messages, findings)

    if args.format == "json":
        print(render_json_summary(messages, findings, output_paths))
    else:
        print(render_summary(messages, findings))
        print(f"Wrote report: {output_paths['report']}")
        print(f"Wrote findings: {output_paths['findings']}")
        print(f"Wrote eval cases: {output_paths['eval_cases']}")
    return 0


def _cmd_doctor(_: argparse.Namespace) -> int:
    lines = [
        f"Agent Doctor: {__version__}",
        f"Python: {platform.python_version()} ({sys.executable})",
        "Privacy mode: local-only, read-only by default, no network calls",
        "Default paths:",
        f"  Hermes: {DEFAULT_HERMES_PATH} (exists: {_exists(DEFAULT_HERMES_PATH)})",
        f"  OpenClaw: {DEFAULT_OPENCLAW_PATH} (exists: {_exists(DEFAULT_OPENCLAW_PATH)})",
    ]
    print("\n".join(lines))
    return 0


def _cmd_install_skill(args: argparse.Namespace) -> int:
    path = install_skill(args.target, args.out)
    print(f"Wrote {path}")
    return 0


def _cmd_mcp(_: argparse.Namespace) -> int:
    print(json.dumps(placeholder_payload(), indent=2))
    return 0


def _exists(path: Path) -> str:
    return "yes" if path.expanduser().exists() else "no"


if __name__ == "__main__":
    raise SystemExit(main())
