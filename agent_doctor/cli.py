"""Command-line interface for Agent Doctor.

The CLI is intentionally thin — every command delegates to a module that's
also useful as a Python API. The commands fall into three groups:

- Diagnosis (``scan``) — local-only, deterministic, no network.
- Patch staging (``apply``) — reads ``findings.json`` and emits a staging
  directory of reviewable patch files plus a unified diff vs. the live agent
  config. Nothing is applied automatically.
- Eval harness (``eval``) — generates synthetic transcripts from scenario
  cards, runs detection benchmarks, and (with an API key) drives the LLM
  judge / replay loop documented in ``docs/evaluation.md``.

The ``doctor``, ``install-skill``, and ``mcp`` commands round out the surface.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from pathlib import Path

from . import __version__
from .apply import load_findings, render_apply_summary, stage_patches
from .autopilot import run_autopilot_once
from .bootstrap import bootstrap, render_bootstrap_summary
from .detectors import detect_findings
from .ingest import (
    DEFAULT_HERMES_PATH,
    DEFAULT_OPENCLAW_PATH,
    IngestError,
    ingest_path_with_errors,
)
from .install import VALID_TARGETS, install_skill
from .mcp import placeholder_payload, serve as serve_mcp
from .report import render_json_summary, render_summary, write_reports


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-doctor",
        description="Turn frustrating agent sessions into durable fixes.",
    )
    parser.add_argument("--version", action="version", version=f"agent-doctor {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="Scan JSONL transcript files.")
    scan.add_argument("--path", type=Path, help="JSONL file or directory containing JSONL files.")
    scan.add_argument("--hermes", action="store_true", help="Use Hermes default session path.")
    scan.add_argument("--openclaw", action="store_true", help="Use OpenClaw default session path.")
    scan.add_argument("--format", choices=["markdown", "json"], default="markdown")
    scan.add_argument("--out", type=Path, required=True, help="Output directory.")
    scan.add_argument(
        "--strict",
        action="store_true",
        help="Fail on malformed JSONL lines instead of skipping them.",
    )
    scan.set_defaults(func=_cmd_scan)

    apply = subparsers.add_parser(
        "apply",
        help="Stage reviewable patches from a previous scan; nothing is applied automatically.",
    )
    apply.add_argument(
        "--findings",
        type=Path,
        required=True,
        help="Path to a findings.json (or a scan output directory).",
    )
    apply.add_argument("--out", type=Path, required=True, help="Staging directory.")
    apply.add_argument(
        "--target",
        type=Path,
        help="Optional live config directory; a unified diff is written to DIFF.txt.",
    )
    apply.add_argument(
        "--min-severity",
        choices=["low", "medium", "high"],
        default="low",
        help="Skip findings below this severity (default: low).",
    )
    apply.add_argument(
        "--min-count",
        type=int,
        default=1,
        help="Skip findings whose aggregated count is below this threshold (default: 1).",
    )
    apply.set_defaults(func=_cmd_apply)

    doctor = subparsers.add_parser("doctor", help="Print environment and readiness info.")
    doctor.set_defaults(func=_cmd_doctor)

    autopilot = subparsers.add_parser(
        "autopilot",
        help="Run the platform-agnostic sidecar trigger engine without host runtime hooks.",
    )
    autopilot.add_argument(
        "--platform",
        choices=["openclaw", "hermes", "generic"],
        required=True,
        help="Host transcript adapter to use. This only selects read-only defaults.",
    )
    autopilot.add_argument(
        "--path",
        type=Path,
        help="Override transcript JSONL file/directory. Required for generic.",
    )
    autopilot.add_argument("--out", type=Path, required=True, help="Autopilot artifact directory.")
    autopilot.add_argument(
        "--state",
        type=Path,
        help="SQLite state path. Defaults to <out>/state.sqlite3.",
    )
    autopilot.add_argument(
        "--watch",
        action="store_true",
        help="Keep polling instead of running one pass.",
    )
    autopilot.add_argument(
        "--interval",
        type=float,
        default=15.0,
        help="Polling interval in seconds when --watch is set.",
    )
    autopilot.add_argument(
        "--cooldown-seconds",
        type=int,
        default=3600,
        help="Suppress repeated notifications for the same session/trigger.",
    )
    autopilot.add_argument(
        "--min-severity",
        choices=["low", "medium", "high"],
        default="medium",
        help="Minimum trigger severity to emit.",
    )
    autopilot.add_argument(
        "--notify-command",
        help="Optional local command to run after emitting a card. Event metadata is passed in AGENT_DOCTOR_* env vars.",
    )
    autopilot.add_argument(
        "--inbox-dir",
        type=Path,
        help="Optional directory for per-session advisory files that agents can read.",
    )
    autopilot.add_argument(
        "--changed-only",
        action="store_true",
        help="Only scan JSONL files whose mtime/size changed since the last run.",
    )
    autopilot.set_defaults(func=_cmd_autopilot)

    setup = subparsers.add_parser(
        "setup",
        help="Opinionated setup flows for agents installing Agent Doctor for users.",
    )
    setup_subs = setup.add_subparsers(dest="setup_command", required=True)
    setup_autopilot = setup_subs.add_parser(
        "autopilot",
        help="Auto-detect OpenClaw/Hermes, install skills, and start autopilot sidecar services.",
    )
    setup_autopilot.add_argument(
        "--platform",
        action="append",
        choices=["openclaw", "hermes"],
        help="Limit setup to one platform. Repeat to install both. Defaults to all detected platforms.",
    )
    setup_autopilot.add_argument(
        "--out-root",
        type=Path,
        help="Root artifact directory. Defaults to ~/.agent-doctor.",
    )
    setup_autopilot.add_argument(
        "--inbox-root",
        type=Path,
        help="Root inbox directory. Defaults to <out-root>/inbox.",
    )
    setup_autopilot.add_argument("--interval", type=float, default=15.0)
    setup_autopilot.add_argument("--cooldown-seconds", type=int, default=3600)
    setup_autopilot.add_argument("--min-severity", choices=["low", "medium", "high"], default="high")
    setup_autopilot.add_argument("--notify-command")
    setup_autopilot.add_argument(
        "--no-start",
        action="store_true",
        help="Write services but do not start/enable them.",
    )
    setup_autopilot.add_argument(
        "--no-bootstrap",
        action="store_true",
        help="Do not install/update Agent Doctor skills before installing services.",
    )
    setup_autopilot.add_argument(
        "--no-invalidate-cache",
        action="store_true",
        help="Do not best-effort invalidate host skill caches after bootstrap.",
    )
    setup_autopilot.add_argument(
        "--no-baseline-existing",
        action="store_true",
        help="Do not mark existing transcript files as already seen before starting services.",
    )
    setup_autopilot.add_argument(
        "--force",
        action="store_true",
        help="Install even if the host root was not auto-detected.",
    )
    setup_autopilot.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be installed without writing files.",
    )
    setup_autopilot.set_defaults(func=_cmd_setup_autopilot)

    service = subparsers.add_parser(
        "service",
        help="Install or manage the local autopilot sidecar service.",
    )
    service_subs = service.add_subparsers(dest="service_command", required=True)
    service_install = service_subs.add_parser(
        "install",
        help="Write a launchd/systemd user service for `agent-doctor autopilot --watch`.",
    )
    service_install.add_argument("--platform", choices=["openclaw", "hermes", "generic"], required=True)
    service_install.add_argument("--path", type=Path, help="Transcript path override; required for generic.")
    service_install.add_argument("--out", type=Path, required=True, help="Autopilot artifact directory.")
    service_install.add_argument("--interval", type=float, default=15.0)
    service_install.add_argument("--cooldown-seconds", type=int, default=3600)
    service_install.add_argument("--min-severity", choices=["low", "medium", "high"], default="medium")
    service_install.add_argument("--notify-command")
    service_install.add_argument("--inbox-dir", type=Path)
    service_install.add_argument("--name", help="Service name suffix. Defaults to platform.")
    service_install.add_argument(
        "--no-baseline-existing",
        action="store_true",
        help="Do not mark existing transcript files as already seen before starting the service.",
    )
    service_install.add_argument("--start", action="store_true", help="Start/enable the service after writing it.")
    service_install.set_defaults(func=_cmd_service_install)

    service_status = service_subs.add_parser("status", help="Print expected service file locations.")
    service_status.add_argument("--platform", choices=["openclaw", "hermes", "generic"], required=True)
    service_status.add_argument("--name")
    service_status.set_defaults(func=_cmd_service_status)

    notify = subparsers.add_parser(
        "notify",
        help="Host-native delivery adapters for autopilot events.",
    )
    notify_subs = notify.add_subparsers(dest="notify_command_name", required=True)
    openclaw_event = notify_subs.add_parser(
        "openclaw-system-event",
        help="Inject an autopilot intervention into OpenClaw via `openclaw system event`.",
    )
    openclaw_event.add_argument("--openclaw-bin", default="openclaw")
    openclaw_event.add_argument("--mode", choices=["now", "next-heartbeat"], default="now")
    openclaw_event.add_argument("--timeout-ms", type=int, default=30000)
    openclaw_event.add_argument("--include-card-chars", type=int, default=6000)
    openclaw_event.add_argument(
        "--all-actions",
        action="store_true",
        help="Deliver notify events too. By default only action=intervene wakes OpenClaw.",
    )
    openclaw_event.add_argument("--dry-run", action="store_true")
    openclaw_event.set_defaults(func=_cmd_notify_openclaw_system_event)

    # Adapter subcommands ------------------------------------------------------
    adapters = subparsers.add_parser(
        "adapters",
        help="Inspect host adapters and their capabilities.",
    )
    adapters_subs = adapters.add_subparsers(dest="adapters_cmd", required=True)

    adapters_list = adapters_subs.add_parser(
        "list",
        help="List detected adapters with capability matrix.",
    )
    adapters_list.add_argument("--json", action="store_true", help="Output as JSON")
    adapters_list.set_defaults(func=_cmd_adapters_list)

    adapters_test = adapters_subs.add_parser(
        "test",
        help="Run contract checks on one adapter.",
    )
    adapters_test.add_argument("host", choices=["openclaw", "hermes", "generic"])
    adapters_test.set_defaults(func=_cmd_adapters_test)

    install = subparsers.add_parser("install-skill", help="Generate a safe host-agent SOP file.")
    install.add_argument(
        "--target",
        choices=sorted(VALID_TARGETS),
        required=True,
        help="Host agent ecosystem to install for.",
    )
    install.add_argument("--out", type=Path, required=True)
    install.set_defaults(func=_cmd_install_skill)

    boot = subparsers.add_parser(
        "bootstrap",
        help="Auto-detect installed agent frameworks and write skill files into each.",
    )
    boot.add_argument(
        "--target",
        action="append",
        default=[],
        choices=sorted(VALID_TARGETS),
        help="Force-install for a specific target even if not auto-detected (repeatable).",
    )
    boot.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be written without touching the filesystem.",
    )
    boot.add_argument(
        "--force",
        action="store_true",
        help="Install for hosts whose home directory does not exist yet.",
    )
    boot.add_argument(
        "--invalidate-cache",
        action="store_true",
        help="Best-effort invalidate host skill caches so the new SKILL.md is picked up without a manual restart.",
    )
    boot.set_defaults(func=_cmd_bootstrap)

    mcp = subparsers.add_parser(
        "mcp",
        help="MCP integration: print metadata, or run the stdio server with `mcp serve`.",
    )
    mcp_subs = mcp.add_subparsers(dest="mcp_command")
    mcp_serve = mcp_subs.add_parser(
        "serve",
        help="Run the MCP stdio server (requires the `mcp` extra).",
    )
    mcp_serve.set_defaults(func=_cmd_mcp_serve)
    mcp.set_defaults(func=_cmd_mcp)

    eval_parser = subparsers.add_parser("eval", help="Eval harness: generate, bench, replay.")
    eval_subs = eval_parser.add_subparsers(dest="eval_command", required=True)

    eval_generate = eval_subs.add_parser(
        "generate",
        help="Generate synthetic transcripts from scenario cards (template-based by default).",
    )
    eval_generate.add_argument("--cards", type=Path, required=True, help="Card YAML file or directory.")
    eval_generate.add_argument("--out", type=Path, required=True, help="Corpus output directory.")
    eval_generate.add_argument(
        "--llm",
        action="store_true",
        help="Use the optional LLM-backed generator (requires ANTHROPIC_API_KEY).",
    )
    eval_generate.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Random seed for the template generator (default: 0, deterministic).",
    )
    eval_generate.set_defaults(func=_cmd_eval_generate)

    eval_bench = eval_subs.add_parser(
        "bench",
        help="Run detection against a labeled corpus and emit precision/recall/F1.",
    )
    eval_bench.add_argument("--corpus", type=Path, required=True, help="Corpus directory from eval generate.")
    eval_bench.add_argument("--out", type=Path, required=True, help="Output directory for the benchmark report.")
    eval_bench.add_argument(
        "--gate-precision",
        type=float,
        default=None,
        help="Exit non-zero if any per-mode precision falls below this value.",
    )
    eval_bench.add_argument(
        "--gate-recall",
        type=float,
        default=None,
        help="Exit non-zero if any per-mode recall falls below this value.",
    )
    eval_bench.set_defaults(func=_cmd_eval_bench)

    eval_replay = eval_subs.add_parser(
        "replay",
        help="Replay a transcript against a patched agent (requires ANTHROPIC_API_KEY).",
    )
    eval_replay.add_argument("--transcript", type=Path, required=True)
    eval_replay.add_argument("--patches", type=Path, required=True, help="Staging directory from apply.")
    eval_replay.add_argument("--out", type=Path, required=True)
    eval_replay.add_argument(
        "--model",
        default="claude-sonnet-4-6",
        help="Model id used for the patched-agent replay.",
    )
    eval_replay.set_defaults(func=_cmd_eval_replay)

    # Closed-loop commands ------------------------------------------------
    approve = subparsers.add_parser(
        "approve",
        help="Approve a pending proposal (CLI fallback for ✅).",
    )
    approve.add_argument("proposal_id")
    approve.set_defaults(func=_cmd_approve)

    dismiss = subparsers.add_parser(
        "dismiss",
        help="Dismiss a pending proposal (CLI fallback for ❌).",
    )
    dismiss.add_argument("proposal_id")
    dismiss.set_defaults(func=_cmd_dismiss)

    redraft = subparsers.add_parser(
        "redraft",
        help="Mark a proposal for redraft (CLI fallback for 💬).",
    )
    redraft.add_argument("proposal_id")
    redraft.set_defaults(func=_cmd_redraft)

    undo = subparsers.add_parser(
        "undo",
        help="Undo an applied patch.",
    )
    undo.add_argument("patch_id", nargs="?")
    undo.add_argument("--last", action="store_true",
                      help="Undo the most recently applied patch.")
    undo.add_argument("--since",
                      help="Undo all patches applied since <duration> (TBD).")
    undo.set_defaults(func=_cmd_undo)

    patches = subparsers.add_parser(
        "patches",
        help="Inspect applied patches.",
    )
    patches_subs = patches.add_subparsers(dest="patches_cmd", required=True)
    patches_list = patches_subs.add_parser(
        "list",
        help="List applied patches with origin and undo command.",
    )
    patches_list.add_argument("--json", action="store_true")
    patches_list.set_defaults(func=_cmd_patches_list)

    digest = subparsers.add_parser(
        "digest",
        help="Compute and (optionally) post a weekly digest.",
    )
    digest.add_argument(
        "--now",
        action="store_true",
        help="Compute and print/post immediately (default for v1).",
    )
    digest.add_argument(
        "--host",
        default="openclaw",
        choices=["openclaw", "hermes", "generic"],
    )
    digest.add_argument(
        "--no-post",
        action="store_true",
        help="Print digest but don't post to a channel.",
    )
    digest.set_defaults(func=_cmd_digest)

    # Calibration subcommands (Tier 3) ----------------------------------------
    calibrate = subparsers.add_parser(
        "calibrate",
        help="Manage Tier 3 calibration (weekly opt-in LLM judges past transcripts).",
    )
    calibrate_subs = calibrate.add_subparsers(dest="calibrate_cmd", required=True)
    calibrate_subs.add_parser("enable", help="Enable Tier 3 calibration.").set_defaults(func=_cmd_calibrate_enable)
    calibrate_subs.add_parser("disable", help="Disable Tier 3 calibration.").set_defaults(func=_cmd_calibrate_disable)
    calibrate_subs.add_parser("review", help="Review pending calibration suggestions.").set_defaults(func=_cmd_calibrate_review)
    calibrate_status = calibrate_subs.add_parser("status", help="Show whether calibration is enabled.")
    calibrate_status.set_defaults(func=_cmd_calibrate_status)

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

    messages, parse_errors = ingest_path_with_errors(input_path, strict=args.strict)
    findings = detect_findings(messages)
    output_paths = write_reports(args.out, messages, findings, parse_errors=parse_errors)

    if args.format == "json":
        print(render_json_summary(messages, findings, output_paths, parse_errors=parse_errors))
    else:
        print(render_summary(messages, findings, parse_errors=parse_errors))
        print(f"Wrote report: {output_paths['report']}")
        print(f"Wrote findings: {output_paths['findings']}")
        print(f"Wrote eval cases: {output_paths['eval_cases']}")
        _print_first_run_hint_if_needed()
    return 0


def _print_first_run_hint_if_needed() -> None:
    """If no SKILL.md is installed in any host, hint the user to bootstrap.

    The hint is printed to stderr so it doesn't pollute machine-readable
    pipelines, and is suppressed when running under JSON-format scans or
    when the file has been installed at least once.
    """

    try:
        from .bootstrap import detect_hosts

        for host in detect_hosts():
            skill_md = host.skill_dir / "agent-doctor" / "SKILL.md"
            categorized_skill_md = host.skill_dir / "autonomous-ai-agents" / "agent-doctor" / "SKILL.md"
            if skill_md.exists() or categorized_skill_md.exists():
                return
        print(
            "\nTip: run `agent-doctor bootstrap --invalidate-cache` so your AI agent\n"
            "     can run this for you next time without you typing the command.",
            file=sys.stderr,
        )
    except Exception:  # pragma: no cover — never let the hint crash the CLI
        return


def _cmd_apply(args: argparse.Namespace) -> int:
    findings_path = args.findings
    if findings_path.is_dir():
        findings_path = findings_path / "findings.json"
    findings = load_findings(findings_path)
    result = stage_patches(
        findings,
        args.out,
        target_dir=args.target,
        minimum_severity=args.min_severity,
        minimum_count=args.min_count,
    )
    print(render_apply_summary(result))
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


def _cmd_autopilot(args: argparse.Namespace) -> int:
    if args.platform == "generic" and args.path is None:
        raise ValueError("generic autopilot requires --path.")

    first_watch_pass = True

    def run_once(*, changed_only: bool) -> None:
        result = run_autopilot_once(
            platform=args.platform,
            path=args.path,
            out_dir=args.out,
            state_path=args.state,
            cooldown_seconds=args.cooldown_seconds,
            min_severity=args.min_severity,
            notify_command=args.notify_command,
            inbox_dir=args.inbox_dir,
            changed_only=changed_only,
        )
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))

    if not args.watch:
        run_once(changed_only=args.changed_only)
        return 0

    if args.interval <= 0:
        raise ValueError("--interval must be positive.")
    while True:
        run_once(changed_only=args.changed_only or not first_watch_pass)
        first_watch_pass = False
        time.sleep(args.interval)


def _cmd_service_install(args: argparse.Namespace) -> int:
    if args.platform == "generic" and args.path is None:
        raise ValueError("generic service install requires --path.")
    from .service import install_sidecar_service, render_service_result

    result = install_sidecar_service(
        platform=args.platform,
        out_dir=args.out,
        transcript_path=args.path,
        interval=args.interval,
        cooldown_seconds=args.cooldown_seconds,
        min_severity=args.min_severity,
        notify_command=args.notify_command,
        inbox_dir=args.inbox_dir,
        name=args.name,
        start=args.start,
        baseline_existing=not args.no_baseline_existing,
    )
    print(render_service_result(result))
    return 0


def _cmd_setup_autopilot(args: argparse.Namespace) -> int:
    from .setup import render_autopilot_setup_result, setup_autopilot

    result = setup_autopilot(
        platforms=args.platform,
        out_root=args.out_root,
        inbox_root=args.inbox_root,
        start=not args.no_start,
        bootstrap_hosts=not args.no_bootstrap,
        invalidate_cache=not args.no_invalidate_cache,
        force=args.force,
        dry_run=args.dry_run,
        interval=args.interval,
        cooldown_seconds=args.cooldown_seconds,
        min_severity=args.min_severity,
        notify_command=args.notify_command,
        baseline_existing=not args.no_baseline_existing,
    )
    print(render_autopilot_setup_result(result))
    return 0


def _cmd_service_status(args: argparse.Namespace) -> int:
    from .service import expected_service_path

    path = expected_service_path(platform=args.platform, name=args.name)
    print(json.dumps({"platform": args.platform, "service_file": str(path), "exists": path.exists()}, indent=2))
    return 0


def _cmd_notify_openclaw_system_event(args: argparse.Namespace) -> int:
    from .delivery import notify_openclaw_system_event

    try:
        result = notify_openclaw_system_event(
            openclaw_bin=args.openclaw_bin,
            mode=args.mode,
            timeout_ms=args.timeout_ms,
            include_card_chars=args.include_card_chars,
            all_actions=args.all_actions,
            dry_run=args.dry_run,
        )
    except RuntimeError as exc:
        print(f"agent-doctor: error: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "delivered": result.delivered,
                "skipped": result.skipped,
                "command": result.command,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def _cmd_adapters_list(args: argparse.Namespace) -> int:
    """Print the capability matrix for every detected host on this machine."""
    from .capabilities import detect_hosts

    hosts = detect_hosts(use_cache=False)
    payload = []
    for adapter in hosts:
        caps = adapter.capabilities()
        payload.append(
            {
                "host_name": caps.host_name,
                "detected_at": str(caps.detected_at),
                "can_send_message": caps.can_send_message,
                "can_edit_message": caps.can_edit_message,
                "can_react": caps.can_react,
                "can_list_reactions": caps.can_list_reactions,
                "can_inject_system_event": caps.can_inject_system_event,
                "can_infer_text": caps.can_infer_text,
                "can_infer_embedding": caps.can_infer_embedding,
                "default_inference_model": caps.default_inference_model,
                "available_channels": list(caps.available_channels),
                "skill_dir": str(caps.skill_dir) if caps.skill_dir else None,
                "memory_writable": str(caps.memory_writable) if caps.memory_writable else None,
                "identity_writable": str(caps.identity_writable) if caps.identity_writable else None,
            }
        )
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for item in payload:
            print(f"\n=== {item['host_name']} ===")
            for k, v in item.items():
                if k == "host_name":
                    continue
                print(f"  {k}: {v}")
    return 0


def _cmd_adapters_test(args: argparse.Namespace) -> int:
    """Detect the named adapter; print its capability matrix; rc=0 on success.

    rc=2: argparse already exits with rc=2 on choice mismatch (we don't reach here).
    rc=3: host not detected on this machine.
    """
    from .adapters import GenericAdapter, HermesAdapter, OpenClawAdapter

    by_name = {
        "generic": GenericAdapter,
        "hermes": HermesAdapter,
        "openclaw": OpenClawAdapter,
    }
    cls = by_name[args.host]  # argparse's choices guarantees presence
    instance = cls.detect()
    if instance is None:
        print(f"{args.host} not detected on this machine.", file=sys.stderr)
        return 3
    caps = instance.capabilities()
    print(f"{caps.host_name} detected at {caps.detected_at}")
    print(f"  can_send_message: {caps.can_send_message}")
    print(f"  can_react:        {caps.can_react}")
    print(f"  can_inject_system_event: {caps.can_inject_system_event}")
    print(f"  can_infer_text:   {caps.can_infer_text}")
    print(f"  available_channels: {list(caps.available_channels)}")
    print(f"  skill_dir:        {caps.skill_dir}")
    return 0


def _cmd_install_skill(args: argparse.Namespace) -> int:
    path = install_skill(args.target, args.out)
    print(f"Wrote {path}")
    return 0


def _cmd_bootstrap(args: argparse.Namespace) -> int:
    result = bootstrap(
        extra_targets=args.target or None,
        dry_run=args.dry_run,
        force=args.force,
        invalidate_cache=args.invalidate_cache,
    )
    print(render_bootstrap_summary(result))
    return 0


def _cmd_mcp(_: argparse.Namespace) -> int:
    print(json.dumps(placeholder_payload(), indent=2))
    return 0


def _cmd_mcp_serve(_: argparse.Namespace) -> int:
    try:
        serve_mcp()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


def _cmd_eval_generate(args: argparse.Namespace) -> int:
    from .evals.generator import generate_corpus

    summary = generate_corpus(args.cards, args.out, use_llm=args.llm, seed=args.seed)
    print(json.dumps(summary, indent=2))
    return 0


def _cmd_eval_bench(args: argparse.Namespace) -> int:
    from .evals.bench import run_benchmark

    result = run_benchmark(args.corpus, args.out)
    print(json.dumps(result.to_summary(), indent=2))

    failures: list[str] = []
    if args.gate_precision is not None:
        for mode, metrics in result.per_mode.items():
            if metrics["precision"] < args.gate_precision:
                failures.append(
                    f"precision gate failed for {mode}: "
                    f"{metrics['precision']:.2f} < {args.gate_precision}"
                )
    if args.gate_recall is not None:
        for mode, metrics in result.per_mode.items():
            if metrics["recall"] < args.gate_recall:
                failures.append(
                    f"recall gate failed for {mode}: "
                    f"{metrics['recall']:.2f} < {args.gate_recall}"
                )
    if failures:
        for line in failures:
            print(line, file=sys.stderr)
        return 1
    return 0


def _cmd_eval_replay(args: argparse.Namespace) -> int:
    from .evals.replay import run_replay

    summary = run_replay(args.transcript, args.patches, args.out, model=args.model)
    print(json.dumps(summary, indent=2))
    return 0


def _find_proposal_across_hosts(proposal_id: str) -> tuple | None:
    """Search all known host proposal files for an id.

    Returns (Proposal, proposals_path) on hit, None on miss.
    """
    from .proposer import load_proposals

    base = Path("~/.agent-doctor").expanduser()
    if not base.exists():
        return None
    for host_dir in base.iterdir():
        if not host_dir.is_dir():
            continue
        path = host_dir / "proposals.jsonl"
        if not path.exists():
            continue
        for proposal in load_proposals(path):
            if proposal.id == proposal_id:
                return (proposal, path)
    return None


def _rewrite_proposal_state(proposals_path: Path, proposal_id: str, new_state: str) -> None:
    """Rewrite proposals.jsonl with the given proposal's state changed."""
    from dataclasses import replace as _dc_replace

    from .proposer import load_proposals

    proposals = load_proposals(proposals_path)
    new_lines: list[str] = []
    for p in proposals:
        if p.id == proposal_id:
            p = _dc_replace(p, state=new_state, resolved_at=time.time())
        new_lines.append(json.dumps(p.to_dict(), ensure_ascii=False))
    fd = os.open(proposals_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as h:
            h.write("\n".join(new_lines) + ("\n" if new_lines else ""))
    finally:
        os.chmod(proposals_path, 0o600)


def _cmd_approve(args: argparse.Namespace) -> int:
    """Apply a pending proposal via the applier."""
    from .adapters import GenericAdapter, HermesAdapter, OpenClawAdapter
    from .applier import apply_proposal as _apply

    found = _find_proposal_across_hosts(args.proposal_id)
    if found is None:
        print(f"approve: proposal {args.proposal_id!r} not found", file=sys.stderr)
        return 1
    proposal, proposals_path = found
    if proposal.state != "pending":
        print(
            f"approve: proposal {args.proposal_id} is already {proposal.state}",
            file=sys.stderr,
        )
        return 1

    adapter_classes = {
        "openclaw": OpenClawAdapter,
        "hermes": HermesAdapter,
        "generic": GenericAdapter,
    }
    cls = adapter_classes.get(proposal.target_host or "generic", GenericAdapter)
    instance = cls.detect() or GenericAdapter()

    result = _apply(proposal, instance)
    if result.state != "applied":
        print(
            f"approve: apply produced state={result.state} "
            f"error={result.error or 'unknown'}",
            file=sys.stderr,
        )
        if result.state == "conflict":
            _rewrite_proposal_state(proposals_path, proposal.id, "conflict")
        return 1

    # Patch log
    log_path = Path("~/.agent-doctor").expanduser() / "patch-log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "id": result.patch_id,
        "target_file": str(result.target_file),
        "backup_path": str(result.backup_path) if result.backup_path else None,
        "applied_at": time.time(),
        "session_id": proposal.session_id,
        "target_kind": proposal.target_kind,
        "undo_command": f"agent-doctor undo {result.patch_id}",
    }
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as h:
            h.write(json.dumps(payload, ensure_ascii=False) + "\n")
    finally:
        os.chmod(log_path, 0o600)

    _rewrite_proposal_state(proposals_path, proposal.id, "applied")
    print(
        f"approve: applied. wrote {result.target_file}; backup {result.backup_path}; "
        f"undo with: agent-doctor undo {result.patch_id}"
    )
    return 0


def _cmd_dismiss(args: argparse.Namespace) -> int:
    found = _find_proposal_across_hosts(args.proposal_id)
    if found is None:
        print(f"dismiss: proposal {args.proposal_id!r} not found", file=sys.stderr)
        return 1
    proposal, proposals_path = found
    if proposal.state != "pending":
        print(
            f"dismiss: proposal {args.proposal_id} is already {proposal.state}",
            file=sys.stderr,
        )
        return 1
    _rewrite_proposal_state(proposals_path, proposal.id, "dismissed")
    print(f"dismiss: proposal {proposal.id} marked dismissed")
    return 0


def _cmd_redraft(args: argparse.Namespace) -> int:
    found = _find_proposal_across_hosts(args.proposal_id)
    if found is None:
        print(f"redraft: proposal {args.proposal_id!r} not found", file=sys.stderr)
        return 1
    proposal, proposals_path = found
    if proposal.state != "pending":
        print(
            f"redraft: proposal {args.proposal_id} is already {proposal.state}",
            file=sys.stderr,
        )
        return 1
    _rewrite_proposal_state(proposals_path, proposal.id, "refining")
    print(
        f"redraft: proposal {proposal.id} marked refining. "
        f"Next user message in this session will trigger a redraft."
    )
    return 0


def _cmd_undo(args: argparse.Namespace) -> int:
    """Restore a patch's target file from its backup."""
    from .applier import undo_patch

    log_path = Path("~/.agent-doctor").expanduser() / "patch-log.jsonl"
    if not log_path.exists():
        print(f"No applied patches found at {log_path}", file=sys.stderr)
        return 1
    entries: list[dict] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    target_id = args.patch_id
    if args.last:
        if not entries:
            print("No applied patches.", file=sys.stderr)
            return 1
        target_id = entries[-1]["id"]
    if not target_id:
        print("Provide a patch_id, --last, or --since.", file=sys.stderr)
        return 2

    matching = [e for e in entries if e.get("id") == target_id]
    if not matching:
        print(f"No patch with id {target_id!r} in patch-log", file=sys.stderr)
        return 1
    entry = matching[-1]
    try:
        undo_patch(
            patch_id=entry["id"],
            backup_path=Path(entry["backup_path"]),
            target_file=Path(entry["target_file"]),
        )
    except RuntimeError as exc:
        print(f"undo failed: {exc}", file=sys.stderr)
        return 1
    print(f"undone: {entry['target_file']} restored from {entry['backup_path']}")
    return 0


def _cmd_patches_list(args: argparse.Namespace) -> int:
    log_path = Path("~/.agent-doctor").expanduser() / "patch-log.jsonl"
    if not log_path.exists():
        if args.json:
            print("[]")
        else:
            print("No applied patches.")
        return 0
    entries: list[dict] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if args.json:
        print(json.dumps(entries, indent=2))
    else:
        for e in entries:
            print(f"{e.get('id', '?')}  {e.get('target_file', '?')}  applied_at={e.get('applied_at', '?')}")
    return 0


def _cmd_digest(args: argparse.Namespace) -> int:
    """Build and (optionally) post a weekly digest."""
    from .digester import build_weekly_digest
    from .speaker import render_digest

    d = build_weekly_digest(args.host)
    body = render_digest(
        events=d.events,
        proposed=d.proposed,
        applied=d.applied,
        measured_better=d.measured_better,
        top_patterns=d.top_patterns,
        language="en",  # digest is en-only for v1
    )
    print(body.render())

    if args.no_post:
        return 0

    # Best-effort post via the host adapter (skip cleanly if unavailable)
    try:
        from .adapters import (
            GenericAdapter,
            HermesAdapter,
            MessageKind,
            OpenClawAdapter,
            Target,
        )
    except ImportError:
        return 0

    adapter_classes = {
        "openclaw": OpenClawAdapter,
        "hermes": HermesAdapter,
        "generic": GenericAdapter,
    }
    cls = adapter_classes[args.host]
    instance = cls.detect()
    if instance is None:
        return 0  # host not detected; printed-only

    inbox = Path("~/.agent-doctor").expanduser() / args.host / "inbox" / "weekly-digest.md"
    target = Target(host=args.host, channel="inbox", recipient="", inbox_path=inbox)
    try:
        instance.send_message(target, body, MessageKind.digest)
        print(f"\nposted to {inbox}")
    except (NotImplementedError, RuntimeError) as exc:
        print(f"\nnot posted: {exc}", file=sys.stderr)
    return 0


# --- Tier 3 calibration -----------------------------------------------------
# v1: opt-in via a flag file. Real weekly cron + LLM judging is a follow-up.

_CALIBRATE_FLAG = Path("~/.agent-doctor/calibrate-enabled").expanduser()
_CALIBRATE_SUGGESTIONS = Path("~/.agent-doctor/calibration-suggestions.md").expanduser()


def _cmd_calibrate_enable(args: argparse.Namespace) -> int:
    _CALIBRATE_FLAG.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _CALIBRATE_FLAG.write_text("enabled\n", encoding="utf-8")
    _CALIBRATE_FLAG.chmod(0o600)
    print(
        "Tier 3 calibration enabled. Weekly batch will judge the past 7 days "
        "of transcripts and write suggestions to "
        f"{_CALIBRATE_SUGGESTIONS}. Review with: agent-doctor calibrate review."
    )
    return 0


def _cmd_calibrate_disable(args: argparse.Namespace) -> int:
    if _CALIBRATE_FLAG.exists():
        _CALIBRATE_FLAG.unlink()
    print("Tier 3 calibration disabled.")
    return 0


def _cmd_calibrate_status(args: argparse.Namespace) -> int:
    enabled = _CALIBRATE_FLAG.exists()
    print(f"calibrate enabled: {enabled}")
    if enabled:
        print(f"flag file: {_CALIBRATE_FLAG}")
        print(f"suggestions file: {_CALIBRATE_SUGGESTIONS} "
              f"({'exists' if _CALIBRATE_SUGGESTIONS.exists() else 'not yet generated'})")
    return 0


def _cmd_calibrate_review(args: argparse.Namespace) -> int:
    if not _CALIBRATE_SUGGESTIONS.exists():
        print("No calibration suggestions yet. Run `agent-doctor calibrate enable` "
              "and wait for the next weekly batch.")
        return 0
    print(_CALIBRATE_SUGGESTIONS.read_text(encoding="utf-8"))
    return 0


def _exists(path: Path) -> str:
    return "yes" if path.expanduser().exists() else "no"


if __name__ == "__main__":
    raise SystemExit(main())
