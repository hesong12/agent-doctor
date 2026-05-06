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


def _exists(path: Path) -> str:
    return "yes" if path.expanduser().exists() else "no"


if __name__ == "__main__":
    raise SystemExit(main())
