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
from .pet import (
    pet_status_for_path,
    pet_status_for_text,
    render_pet_markdown,
    write_pet_artifacts,
)
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

    pet = subparsers.add_parser(
        "pet",
        help="Show the local Agent Doctor state for a transcript or current user message.",
    )
    pet.add_argument("--path", type=Path, help="JSONL file or directory containing JSONL files.")
    pet.add_argument("--message", help="Current user message to diagnose without reading a transcript.")
    pet.add_argument("--session-id", default="manual", help="Session id to use with --message.")
    pet.add_argument("--hermes", action="store_true", help="Use Hermes default session path.")
    pet.add_argument("--openclaw", action="store_true", help="Use OpenClaw default session path.")
    pet.add_argument(
        "--platform",
        choices=["generic", "openclaw", "hermes"],
        default="generic",
        help="Platform label for --path or --message. Default: generic.",
    )
    pet.add_argument("--format", choices=["markdown", "json"], default="markdown")
    pet.add_argument("--out", type=Path, help="Optional directory for pet-status.json and pet-card.md.")
    pet.add_argument(
        "--display",
        action="store_true",
        help="Open an always-on-top desktop Agent Doctor window after writing status.",
    )
    pet.add_argument(
        "--strict",
        action="store_true",
        help="Fail on malformed JSONL lines instead of skipping them.",
    )
    pet.set_defaults(func=_cmd_pet)

    pet_display = subparsers.add_parser(
        "pet-display",
        help="Open the always-on-top Agent Doctor desktop window from pet-status.json.",
    )
    pet_display.add_argument(
        "--status-file",
        type=Path,
        help="pet-status.json to watch. Defaults to ~/.agent-doctor/pet/pet-status.json.",
    )
    pet_display.add_argument("--poll", type=float, default=1.0, help="Refresh interval in seconds.")
    pet_display.add_argument("--not-topmost", action="store_true", help="Do not force the window on top.")
    pet_display.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the status snapshot without opening a desktop window.",
    )
    pet_display.set_defaults(func=_cmd_pet_display)

    pet_action = subparsers.add_parser(
        "pet-action",
        help="Run a local backend action requested by the Agent Doctor frontend.",
    )
    pet_action_subs = pet_action.add_subparsers(dest="pet_action", required=True)
    send_recovery = pet_action_subs.add_parser(
        "send-recovery",
        help="Send the current Agent Doctor recovery suggestion back through the host adapter.",
    )
    send_recovery.add_argument(
        "--status-file",
        type=Path,
        required=True,
        help="pet-status.json containing the incident to route.",
    )
    send_recovery.set_defaults(func=_cmd_pet_action_send_recovery)
    diagnose_current = pet_action_subs.add_parser(
        "diagnose-current",
        help="Check the current OpenClaw/Hermes transcript and refresh Agent Doctor status.",
    )
    diagnose_current.add_argument(
        "--status-file",
        type=Path,
        required=True,
        help="pet-status.json to refresh after diagnosing the current session.",
    )
    diagnose_current.set_defaults(func=_cmd_pet_action_diagnose_current)
    dismiss_current = pet_action_subs.add_parser(
        "dismiss",
        help="Dismiss the current Agent Doctor incident and persist the suppression.",
    )
    dismiss_current.add_argument(
        "--status-file",
        type=Path,
        required=True,
        help="pet-status.json containing the incident to dismiss.",
    )
    dismiss_current.set_defaults(func=_cmd_pet_action_dismiss)

    pet_set_sprite = subparsers.add_parser(
        "pet-set-sprite",
        help="Replace the desktop pet image with your own sprite.",
    )
    pet_set_sprite.add_argument(
        "source",
        type=Path,
        help="Path to the source image (JPG/PNG/WEBP).",
    )
    pet_set_sprite.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Override output path. Default: ~/.agent-doctor/pet/sprite.png.",
    )
    pet_set_sprite.add_argument(
        "--no-bg-removal",
        action="store_true",
        help="Skip the corner floodfill background removal (still resized).",
    )
    pet_set_sprite.set_defaults(func=_cmd_pet_set_sprite)

    pet_generate_sprite = subparsers.add_parser(
        "pet-generate-sprite",
        help=(
            "Generate the desktop pet sprite from a text prompt via Gemini "
            "(Nano Banana 2). Reuses the same transform + atomic write as "
            "pet-set-sprite."
        ),
    )
    pet_generate_sprite.add_argument(
        "--prompt",
        required=True,
        help="Text description of the pet to generate.",
    )
    pet_generate_sprite.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Override output path. Default: ~/.agent-doctor/pet/sprite.png.",
    )
    pet_generate_sprite.add_argument(
        "--no-bg-removal",
        action="store_true",
        help="Skip the corner floodfill background removal (still resized).",
    )
    pet_generate_sprite.set_defaults(func=_cmd_pet_generate_sprite)

    pet_usage = subparsers.add_parser(
        "pet-usage",
        help=(
            "Collect Claude + Codex token usage for the desktop pet popover. "
            "Reads local ccusage / @ccusage/codex output — no live quota API."
        ),
    )
    pet_usage.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Print JSON suitable for the Swift popover. Default: pretty print.",
    )
    pet_usage.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Per-call timeout in seconds for each npx query (default: 15).",
    )
    pet_usage.set_defaults(func=_cmd_pet_usage)

    settings_parser = subparsers.add_parser(
        "settings",
        help="Manage Agent Doctor local settings (Gemini API key).",
    )
    settings_subs = settings_parser.add_subparsers(dest="settings_cmd", required=True)

    settings_set = settings_subs.add_parser(
        "set-gemini-key",
        help=(
            "Store a Gemini API key (read from stdin or --from-env). "
            "The key is NEVER taken from a positional argv argument so it "
            "does not leak into shell history."
        ),
    )
    settings_set.add_argument(
        "--from-env",
        dest="from_env",
        metavar="ENV_VAR",
        default=None,
        help=(
            "Read the key from the named environment variable instead of stdin. "
            "Typical use: GEMINI_API_KEY=... agent-doctor settings set-gemini-key --from-env GEMINI_API_KEY"
        ),
    )
    settings_set.set_defaults(func=_cmd_settings_set_gemini_key)

    settings_clear = settings_subs.add_parser(
        "clear-gemini-key",
        help="Remove the stored Gemini API key from all backends.",
    )
    settings_clear.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help=(
            "Skip interactive confirmation. Required when stdin is not a "
            "TTY — protects user-set keys from accidental clears in scripts, "
            "CI, and smoke tests. Has no effect when no key is currently "
            "configured (clear is a no-op in that case)."
        ),
    )
    settings_clear.set_defaults(func=_cmd_settings_clear_gemini_key)

    settings_show = settings_subs.add_parser(
        "show",
        help="Show which backend stores the key and whether it is configured. Never prints the key.",
    )
    settings_show.set_defaults(func=_cmd_settings_show)

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
        default=2.0,
        help="Polling interval in seconds when --watch is set.",
    )
    autopilot.add_argument(
        "--cooldown-seconds",
        type=int,
        default=3600,
        help="Suppress repeated events for the same session/trigger.",
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
        "--pet-out",
        type=Path,
        help="Optional shared Agent Doctor status directory. Defaults to the autopilot output directory.",
    )
    autopilot.add_argument(
        "--dispatch-adapter",
        action="store_true",
        help="Legacy: also deliver events through host adapters. Default interaction is Agent Doctor only.",
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
        help="Optional root inbox directory for legacy advisory files.",
    )
    setup_autopilot.add_argument("--interval", type=float, default=2.0)
    setup_autopilot.add_argument("--cooldown-seconds", type=int, default=3600)
    setup_autopilot.add_argument("--min-severity", choices=["low", "medium", "high"], default="high")
    setup_autopilot.add_argument(
        "--notify-command",
        help="Legacy explicit hook. By default setup uses Agent Doctor only and does not send system notifications.",
    )
    setup_autopilot.add_argument(
        "--no-desktop-pet",
        action="store_true",
        help="Do not install/start the desktop Agent Doctor service.",
    )
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
    service_install.add_argument("--interval", type=float, default=2.0)
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

    # Dictate subcommand -------------------------------------------------------
    from .dictate import (
        DEFAULT_MODE as _DICTATE_DEFAULT_MODE,
        SUPPORTED_BACKENDS as _DICTATE_SUPPORTED_BACKENDS,
        SUPPORTED_MODES as _DICTATE_SUPPORTED_MODES,
    )

    dictate = subparsers.add_parser(
        "dictate",
        help=(
            "Voice -> optimized-prompt clipboard pipeline. Press a hotkey, speak, "
            "and paste an LLM-rewritten prompt into any AI app."
        ),
    )
    dictate_subs = dictate.add_subparsers(dest="dictate_command", required=True)

    def _add_common_dictate_args(p, *, with_mode=True):
        if with_mode:
            p.add_argument(
                "--mode",
                choices=list(_DICTATE_SUPPORTED_MODES),
                default=None,
                help=(
                    "Prompt-rewriting style. 'raw' skips the LLM and copies the "
                    "transcript verbatim. When omitted, 'stop'/'toggle' reuses "
                    f"the mode the recording was started in (default for new "
                    f"recordings: {_DICTATE_DEFAULT_MODE})."
                ),
            )
        p.add_argument(
            "--no-enhance",
            action="store_true",
            help="Skip the LLM enhancement step; copy the raw transcript.",
        )
        p.add_argument(
            "--whisper-model",
            default=None,
            help=(
                "Whisper model identifier. For faster-whisper: a size alias "
                "('small', 'medium', 'large-v3-turbo') or HF repo id. For "
                "whisper-cpp: an absolute path to a GGML/GGUF file (e.g. "
                "Handy's ggml-large-v3-turbo.bin). Auto-detects backend from "
                "the suffix unless --backend is explicit."
            ),
        )
        p.add_argument(
            "--backend",
            choices=list(_DICTATE_SUPPORTED_BACKENDS),
            default=None,
            help=(
                "Whisper transcription backend. 'auto' (default) routes paths "
                "ending in .bin/.gguf to whisper-cpp and everything else to "
                "faster-whisper. Override with 'faster-whisper' or "
                "'whisper-cpp' to force a backend."
            ),
        )
        p.add_argument(
            "--language",
            default=None,
            help="BCP-47 language hint for whisper (e.g. 'en', 'zh'). Auto-detect by default.",
        )
        p.add_argument("--llm-url", default=None, help="OpenAI-compatible chat completion URL.")
        p.add_argument("--llm-model", default=None, help="Model name to send in the request body.")
        p.add_argument("--llm-key", default=None, help="Optional bearer token for the LLM endpoint.")
        p.add_argument("--keep-audio", action="store_true", help="Do not delete the WAV after processing.")
        p.add_argument("--print-transcript", action="store_true", help="Also print the raw transcript to stderr.")
        p.add_argument(
            "--buffer-ms",
            type=int,
            default=None,
            help=(
                "Extra recording tail in milliseconds (capture audio AFTER 'stop' "
                "is invoked). Avoids cutting the final syllable when releasing the "
                "hotkey. Default: 150 ms; env: AGENT_DOCTOR_DICTATE_BUFFER_MS."
            ),
        )
        beep_group = p.add_mutually_exclusive_group()
        beep_group.add_argument(
            "--beep",
            dest="beep",
            action="store_true",
            default=None,
            help="Play short macOS system sounds on recording start / done / failure.",
        )
        beep_group.add_argument(
            "--no-beep",
            dest="beep",
            action="store_false",
            help="Disable audio feedback even if AGENT_DOCTOR_DICTATE_BEEP=1 is set.",
        )
        p.add_argument(
            "--timing",
            action="store_true",
            help="Print a per-phase millisecond breakdown to stderr.",
        )
        p.add_argument(
            "--no-history",
            action="store_true",
            help="Skip writing this transcription to the SQLite history.",
        )

    dictate_start = dictate_subs.add_parser("start", help="Start a recording.")
    _add_common_dictate_args(dictate_start)
    dictate_start.set_defaults(func=_cmd_dictate_start)

    dictate_stop = dictate_subs.add_parser(
        "stop", help="Stop the running recording, enhance, and copy to clipboard."
    )
    _add_common_dictate_args(dictate_stop, with_mode=False)
    dictate_stop.set_defaults(func=_cmd_dictate_stop)

    dictate_toggle = dictate_subs.add_parser(
        "toggle",
        help="Stop if a recording is in flight, otherwise start one. Bind this to a hotkey.",
    )
    _add_common_dictate_args(dictate_toggle)
    dictate_toggle.set_defaults(func=_cmd_dictate_toggle)

    dictate_status = dictate_subs.add_parser(
        "status", help="Show the current recording state as JSON."
    )
    dictate_status.set_defaults(func=_cmd_dictate_status)

    dictate_cancel = dictate_subs.add_parser(
        "cancel", help="Abort the current recording and discard the audio."
    )
    dictate_cancel.set_defaults(func=_cmd_dictate_cancel)

    dictate_history = dictate_subs.add_parser(
        "history",
        help="Show recent dictate runs (transcript + final prompt) from the SQLite history.",
    )
    dictate_history.add_argument("--limit", type=int, default=20, help="Rows to display (default: 20).")
    dictate_history.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    dictate_history.add_argument(
        "--full",
        action="store_true",
        help="Include the full transcript and prompt; default truncates each to 120 chars.",
    )
    dictate_history.add_argument(
        "--clear",
        action="store_true",
        help="Delete the history database after printing (asks no confirmation).",
    )
    dictate_history.set_defaults(func=_cmd_dictate_history)

    dictate_models = dictate_subs.add_parser(
        "models",
        help="Manage whisper.cpp transcription models (list/download/set/remove/doctor).",
    )
    dictate_models_subs = dictate_models.add_subparsers(
        dest="dictate_models_cmd", required=True
    )

    dm_list = dictate_models_subs.add_parser("list", help="Show catalog + install status.")
    dm_list.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    dm_list.set_defaults(func=_cmd_dictate_models_list)

    dm_current = dictate_models_subs.add_parser("current", help="Print the active model.")
    dm_current.add_argument("--json", action="store_true")
    dm_current.set_defaults(func=_cmd_dictate_models_current)

    dm_download = dictate_models_subs.add_parser(
        "download", help="Download a model from the authorized catalog."
    )
    dm_download.add_argument("model_id", help="Catalog id (see 'list').")
    dm_download.add_argument("--force", action="store_true", help="Re-download even if installed.")
    dm_download.set_defaults(func=_cmd_dictate_models_download)

    dm_set = dictate_models_subs.add_parser(
        "set", help="Mark a downloaded model as the active transcription model."
    )
    dm_set.add_argument("model_id")
    dm_set.set_defaults(func=_cmd_dictate_models_set)

    dm_remove = dictate_models_subs.add_parser(
        "remove", help="Delete the on-disk file for a model id."
    )
    dm_remove.add_argument("model_id")
    dm_remove.set_defaults(func=_cmd_dictate_models_remove)

    dm_doctor = dictate_models_subs.add_parser(
        "doctor", help="Re-verify SHA-256 of all installed models."
    )
    dm_doctor.set_defaults(func=_cmd_dictate_models_doctor)

    dictate_llm = dictate_subs.add_parser(
        "llm",
        help="Configure the LLM enhancer (LM Studio / Ollama / Custom).",
    )
    dictate_llm_subs = dictate_llm.add_subparsers(
        dest="dictate_llm_cmd", required=True
    )

    llm_probe = dictate_llm_subs.add_parser(
        "probe", help="Probe every known provider's /v1/models endpoint."
    )
    llm_probe.add_argument("--json", action="store_true")
    llm_probe.set_defaults(func=_cmd_dictate_llm_probe)

    llm_set = dictate_llm_subs.add_parser("set", help="Update the active LLM provider.")
    llm_set.add_argument("--provider", choices=["lm_studio", "ollama", "custom"], required=True)
    llm_set.add_argument("--model", default=None, help="OpenAI-style model id.")
    llm_set.add_argument("--url", default=None, help="Override base URL (Custom provider only).")
    llm_set.set_defaults(func=_cmd_dictate_llm_set)

    llm_current = dictate_llm_subs.add_parser("current", help="Show active provider + model.")
    llm_current.add_argument("--json", action="store_true")
    llm_current.set_defaults(func=_cmd_dictate_llm_current)

    llm_test = dictate_llm_subs.add_parser(
        "test", help="Round-trip a canned transcript through the configured provider."
    )
    llm_test.add_argument(
        "text", nargs="?", default="please rewrite this as a clean prompt", help="Text to enhance."
    )
    llm_test.set_defaults(func=_cmd_dictate_llm_test)

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


def _cmd_pet(args: argparse.Namespace) -> int:
    selected_inputs = [
        bool(args.path),
        bool(args.message),
        bool(args.hermes),
        bool(args.openclaw),
    ]
    if sum(selected_inputs) != 1:
        raise ValueError("Provide exactly one of --path, --message, --hermes, or --openclaw.")

    platform = args.platform
    if args.hermes:
        status = pet_status_for_path(
            DEFAULT_HERMES_PATH,
            platform="hermes",
            strict=args.strict,
        )
    elif args.openclaw:
        status = pet_status_for_path(
            DEFAULT_OPENCLAW_PATH,
            platform="openclaw",
            strict=args.strict,
        )
    elif args.message:
        status = pet_status_for_text(args.message, platform=platform, session_id=args.session_id)
    else:
        status = pet_status_for_path(args.path, platform=platform, strict=args.strict)

    out_dir = args.out
    if args.display and out_dir is None:
        from .pet_display import default_status_file

        out_dir = default_status_file().parent

    paths: dict[str, Path] = {}
    if out_dir:
        paths = write_pet_artifacts(out_dir, status)

    if args.format == "json":
        payload = status.to_dict()
        if paths:
            payload["outputs"] = {name: str(path) for name, path in paths.items()}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(render_pet_markdown(status))
        if paths:
            print(f"Wrote pet status: {paths['status']}")
            print(f"Wrote pet card: {paths['card']}")
    if args.display:
        from .pet_display import display_pet

        display_pet(paths["status"])
    return 0


def _cmd_pet_display(args: argparse.Namespace) -> int:
    from .pet_display import (
        default_status_file,
        display_pet,
        read_status_payload,
        snapshot_from_payload,
        snapshot_to_dict,
    )

    status_file = args.status_file or default_status_file()
    if args.dry_run:
        snapshot = snapshot_from_payload(read_status_payload(status_file))
        print(json.dumps(snapshot_to_dict(snapshot), indent=2, ensure_ascii=False))
        return 0
    display_pet(
        status_file,
        poll_seconds=args.poll,
        topmost=not args.not_topmost,
    )
    return 0


def _cmd_pet_action_send_recovery(args: argparse.Namespace) -> int:
    from .pet_actions import send_recovery_from_status_file

    result = send_recovery_from_status_file(args.status_file)
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    return 0 if result.delivered else 1


def _cmd_pet_action_diagnose_current(args: argparse.Namespace) -> int:
    from .pet_actions import diagnose_current_from_status_file

    result = diagnose_current_from_status_file(args.status_file)
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    return 0 if result.delivered else 1


def _cmd_pet_action_dismiss(args: argparse.Namespace) -> int:
    from .pet_actions import dismiss_current_from_status_file

    result = dismiss_current_from_status_file(args.status_file)
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    return 0 if result.delivered else 1


def _cmd_pet_set_sprite(args: argparse.Namespace) -> int:
    source: Path = Path(args.source).expanduser()
    if not source.exists():
        print(f"agent-doctor: source image not found: {source}", file=sys.stderr)
        return 2
    if source.is_dir():
        print(
            f"agent-doctor: source is a directory, not an image file: {source}",
            file=sys.stderr,
        )
        return 2

    from .pet_display import user_sprite_path
    from .sprite_pipeline import (
        PillowMissingError,
        transform_image,
        write_sprite_atomic,
    )

    destination: Path = (
        Path(args.out).expanduser() if args.out is not None else user_sprite_path()
    )

    # Phase 1: read + decode the source image. Errors here are about the
    # source — bad format, corrupt file, unreadable permissions, missing.
    try:
        image = transform_image(source, remove_background=not args.no_bg_removal)
    except PillowMissingError as exc:
        print(f"agent-doctor: {exc}", file=sys.stderr)
        return 3
    except FileNotFoundError as exc:
        print(f"agent-doctor: {exc}", file=sys.stderr)
        return 2
    except IsADirectoryError:
        # Defensive: covered by the is_dir() guard above, but a path that
        # becomes a directory between checks would still land here rather
        # than throw a raw traceback.
        print(
            f"agent-doctor: source is a directory, not an image file: {source}",
            file=sys.stderr,
        )
        return 2
    except PermissionError as exc:
        print(
            f"agent-doctor: cannot read source image (permission denied): {exc}",
            file=sys.stderr,
        )
        return 2
    except OSError as exc:
        # PIL.UnidentifiedImageError (an OSError subclass in Python 3) and
        # truncated-decode errors land here. Don't conflate this with
        # write-side I/O failures, which are reported separately below.
        print(
            f"agent-doctor: could not decode image '{source}': {exc}",
            file=sys.stderr,
        )
        return 2

    # Phase 2: atomically write the transformed sprite. Errors here are
    # about the *destination* — full disk, read-only target dir, missing
    # parent permissions — and must not be reported as a decode failure.
    try:
        written = write_sprite_atomic(image, destination)
    except PermissionError as exc:
        print(
            f"agent-doctor: cannot write sprite to {destination} "
            f"(permission denied): {exc}",
            file=sys.stderr,
        )
        return 2
    except OSError as exc:
        print(
            f"agent-doctor: could not write sprite to {destination}: {exc}",
            file=sys.stderr,
        )
        return 2

    print(f"agent-doctor: wrote sprite -> {written}")
    return 0


def _cmd_pet_generate_sprite(args: argparse.Namespace) -> int:
    """Generate the pet sprite from a Gemini prompt and run the same
    pipeline as ``pet-set-sprite``.

    Error handling mirrors ``_cmd_pet_set_sprite`` — separate phases for
    SDK / decode / write — and adds a redaction phase: any exception that
    bubbles up here gets the API key scrubbed before we print to stderr,
    so a noisy SDK traceback can never leak the secret to logs.
    """

    from tempfile import NamedTemporaryFile

    from .pet_display import user_sprite_path
    from .settings import SettingsError, load_gemini_key, redact_secret
    from .sprite_pipeline import (
        PillowMissingError,
        transform_image,
        write_sprite_atomic,
    )

    prompt: str = (args.prompt or "").strip()
    if not prompt:
        print("agent-doctor: --prompt is empty.", file=sys.stderr)
        return 2

    try:
        api_key = load_gemini_key()
    except SettingsError as exc:
        # SettingsError is already redacted at construction time, but pass
        # through redact_secret defensively in case a future caller path
        # supplies the key differently.
        print(f"agent-doctor: {exc}", file=sys.stderr)
        return 4
    if not api_key:
        print(
            "agent-doctor: no Gemini API key is configured. "
            "Run `agent-doctor settings set-gemini-key --from-env GEMINI_API_KEY` first.",
            file=sys.stderr,
        )
        return 4

    # Phase 1: call Gemini. Always redact the key out of any thrown text.
    from .gemini_image import (
        GeminiImageError,
        GeminiSdkMissingError,
        generate_pet_sprite_bytes,
    )

    try:
        image_bytes = generate_pet_sprite_bytes(prompt=prompt, api_key=api_key)
    except GeminiSdkMissingError as exc:
        print(f"agent-doctor: {exc}", file=sys.stderr)
        return 3
    except GeminiImageError as exc:
        print(
            f"agent-doctor: {redact_secret(str(exc), api_key)}",
            file=sys.stderr,
        )
        return 5
    except Exception as exc:
        # Last-resort redaction so an unexpected SDK crash never prints the key.
        print(
            f"agent-doctor: unexpected Gemini error: {redact_secret(str(exc), api_key)}",
            file=sys.stderr,
        )
        return 5

    # Phase 2: write the raw bytes to a temp file so the existing pipeline
    # (which expects a Path) can decode them, then run the unchanged
    # transform_image() + write_sprite_atomic() pair.
    destination: Path = (
        Path(args.out).expanduser() if args.out is not None else user_sprite_path()
    )

    # Bracket the entire temp-file lifecycle (create → write → decode →
    # destination write) in one try/finally so the file is always removed,
    # even if an exception fires between NamedTemporaryFile() and the
    # outer try (e.g. a MemoryError inside tmp_handle.write).
    tmp_handle = NamedTemporaryFile(
        prefix="agent-doctor-gemini-",
        suffix=".bin",
        delete=False,
    )
    tmp_path = Path(tmp_handle.name)
    try:
        try:
            tmp_handle.write(image_bytes)
            tmp_handle.flush()
        finally:
            tmp_handle.close()

        # Phase 2a: decode + transform.
        try:
            image = transform_image(
                tmp_path, remove_background=not args.no_bg_removal
            )
        except PillowMissingError as exc:
            print(f"agent-doctor: {exc}", file=sys.stderr)
            return 3
        except OSError as exc:
            # PIL.UnidentifiedImageError + truncated decode land here.
            print(
                f"agent-doctor: could not decode image bytes returned by Gemini: {exc}",
                file=sys.stderr,
            )
            return 5

        # Phase 2b: atomically write the transformed sprite.
        try:
            written = write_sprite_atomic(image, destination)
        except PermissionError as exc:
            print(
                f"agent-doctor: cannot write sprite to {destination} "
                f"(permission denied): {exc}",
                file=sys.stderr,
            )
            return 2
        except OSError as exc:
            print(
                f"agent-doctor: could not write sprite to {destination}: {exc}",
                file=sys.stderr,
            )
            return 2
    finally:
        tmp_path.unlink(missing_ok=True)

    print(f"agent-doctor: wrote sprite -> {written}")
    return 0


def _cmd_pet_usage(args: argparse.Namespace) -> int:
    """Collect Claude + Codex usage and emit it for the popover.

    The handler intentionally never raises — the desktop pet popover
    relies on getting *some* JSON back so it can render an install-hint
    card even when ``npx``/the packages are missing. Decode and render
    failures are split: a serialization problem reports a separate exit
    code (5) so a future caller can distinguish "no data sources" from
    "data was collected but we couldn't print it".
    """

    from .usage import DEFAULT_TIMEOUT_SECONDS, collect_usage

    timeout = float(args.timeout if args.timeout is not None else DEFAULT_TIMEOUT_SECONDS)
    if timeout <= 0:
        raise ValueError("--timeout must be positive.")

    # Phase 1: collect. The collector swallows source failures and reports
    # them via the .error fields inside the payload; nothing should bubble
    # up here. A genuine programming bug still surfaces via the outer
    # try/except so the pet popover doesn't silently render stale data.
    try:
        payload = collect_usage(timeout=timeout)
    except Exception as exc:  # pragma: no cover — defensive net
        print(f"agent-doctor: usage collection crashed: {exc}", file=sys.stderr)
        return 5

    # Phase 2: serialize. ``json.dumps`` shouldn't fail on a payload we
    # built ourselves, but if a future field is non-serializable we want
    # a separate, debuggable exit so it isn't conflated with source loss.
    try:
        encoded = json.dumps(payload, indent=2, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        print(f"agent-doctor: could not encode usage payload: {exc}", file=sys.stderr)
        return 5

    if args.as_json:
        print(encoded)
    else:
        # Human-readable variant for the CLI; the Swift popover always
        # passes --json so this branch is just for users who run the
        # command directly to check that npx is wired up.
        print(encoded)
    return 0


def _cmd_settings_set_gemini_key(args: argparse.Namespace) -> int:
    from .settings import SettingsError, store_gemini_key

    if args.from_env:
        env_name: str = args.from_env
        value = os.environ.get(env_name)
        if value is None or value == "":
            print(
                f"agent-doctor: environment variable {env_name} is not set or empty.",
                file=sys.stderr,
            )
            return 2
    else:
        if sys.stdin is None or sys.stdin.isatty():
            print(
                "agent-doctor: paste the Gemini API key on stdin (no echo guarantee), "
                "then send EOF (Ctrl-D). For safer entry pass --from-env GEMINI_API_KEY.",
                file=sys.stderr,
            )
        try:
            value = sys.stdin.read()
        except KeyboardInterrupt:
            print("agent-doctor: aborted.", file=sys.stderr)
            return 2

    cleaned = (value or "").strip()
    if not cleaned:
        print("agent-doctor: no key provided.", file=sys.stderr)
        return 2

    try:
        backend = store_gemini_key(cleaned)
    except SettingsError as exc:
        print(f"agent-doctor: {exc}", file=sys.stderr)
        return 5

    # Never echo the key. Confirm only the destination.
    print(f"agent-doctor: stored Gemini API key (backend: {backend.value}).")
    return 0


def _cmd_settings_clear_gemini_key(args: argparse.Namespace) -> int:
    """Clear-gemini-key with destructive-op guard.

    Sequence:
    1. If no key is configured, the call is an idempotent no-op and exits 0.
    2. If ``--yes`` was passed, clear immediately.
    3. Otherwise on a TTY, prompt for the literal string ``clear`` to confirm.
       Anything else aborts with exit 2 and the key intact.
    4. Otherwise (non-TTY without ``--yes``), refuse with exit 2 and an
       error pointing at the flag. This is the guard that catches
       smoke-test / CI / script accidents.
    """

    from .settings import clear_gemini_key, settings_status

    status = settings_status()
    if not status.configured:
        # Idempotent no-op: nothing to confirm, nothing to lose.
        print("agent-doctor: no Gemini API key was stored.")
        return 0

    if not args.yes:
        if sys.stdin.isatty():
            prompt_lines = [
                f"This will permanently remove the Gemini API key from {status.backend.value}.",
            ]
            if status.meta is not None and status.meta.set_at:
                prompt_lines.append(
                    f"Last set: {status.meta.set_at} via {status.meta.caller_executable}"
                )
            prompt_lines.append("Type 'clear' to confirm (anything else aborts): ")
            sys.stderr.write("\n".join(prompt_lines))
            sys.stderr.flush()
            try:
                entered = sys.stdin.readline().strip()
            except KeyboardInterrupt:
                print("\nagent-doctor: aborted.", file=sys.stderr)
                return 2
            if entered != "clear":
                print(
                    "agent-doctor: aborted (confirmation not entered). "
                    "Key unchanged.",
                    file=sys.stderr,
                )
                return 2
        else:
            print(
                "agent-doctor: 'clear-gemini-key' is destructive and requires "
                "explicit confirmation. Re-run with --yes to clear, or run "
                "interactively. No key was modified.",
                file=sys.stderr,
            )
            return 2

    clear_gemini_key()
    print("agent-doctor: cleared Gemini API key.")
    return 0


def _cmd_settings_show(_args: argparse.Namespace) -> int:
    from .settings import settings_status

    print(settings_status().render())
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
            pet_out_dir=args.pet_out,
            dispatch_adapter=args.dispatch_adapter,
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
        desktop_pet=not args.no_desktop_pet,
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


def _maybe_deprecate_mode(mode: Optional[str]) -> Optional[str]:
    """If ``mode`` is one of the deprecated chat/coding/research aliases, emit a
    one-line stderr warning and collapse to 'optimize'. Returns the canonical mode."""

    from . import dictate as _d

    if mode in _d.DEPRECATED_MODES:
        print(
            f"agent-doctor: --mode {mode!r} is deprecated; using 'optimize' "
            "(see CHANGELOG)",
            file=sys.stderr,
        )
        return "optimize"
    return mode


def _cmd_dictate_start(args: argparse.Namespace) -> int:
    from . import dictate as _d

    raw_mode = getattr(args, "mode", None)
    mode = _maybe_deprecate_mode(raw_mode) or _d.DEFAULT_MODE
    try:
        state = _d.start_recording(mode=mode)
    except _d.DictateError as exc:
        print(f"agent-doctor: {exc}", file=sys.stderr)
        if _d.beep_enabled(getattr(args, "beep", None)):
            _d.play_sound(_d.DEFAULT_FAIL_SOUND)
        return 2
    if _d.beep_enabled(getattr(args, "beep", None)):
        _d.play_sound(_d.DEFAULT_START_SOUND)
    print(
        json.dumps(
            {
                "started": True,
                "pid": state.pid,
                "audio_path": state.audio_path,
                "mode": state.mode,
                "recorder": state.recorder,
            },
            indent=2,
        )
    )
    return 0


def _cmd_dictate_stop(args: argparse.Namespace) -> int:
    return _dictate_finish(args)


def _cmd_dictate_toggle(args: argparse.Namespace) -> int:
    from . import dictate as _d

    try:
        state = _d.read_state()
    except _d.DictateError as exc:
        # Corrupt state file: surface the error and exit non-zero so a user can
        # recover with 'dictate cancel'. Do NOT fall through to start_recording
        # because start would re-raise on the same stale file.
        print(f"agent-doctor: {exc}", file=sys.stderr)
        print("agent-doctor: run 'agent-doctor dictate cancel' to clear stale state", file=sys.stderr)
        return 2
    if state is not None and _d.is_pid_alive(state.pid):
        return _dictate_finish(args)
    return _cmd_dictate_start(args)


def _cmd_dictate_status(_args: argparse.Namespace) -> int:
    from . import dictate as _d

    try:
        state = _d.read_state()
    except _d.DictateError as exc:
        print(
            json.dumps(
                {
                    "recording": False,
                    "error": str(exc),
                    "hint": "run 'agent-doctor dictate cancel' to clear stale state",
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(_d.summarize_state(state), indent=2, sort_keys=True))
    return 0


def _cmd_dictate_cancel(_args: argparse.Namespace) -> int:
    from . import dictate as _d

    try:
        state = _d.read_state()
    except _d.DictateError as exc:
        # Corrupt state file: best-effort clear and inform the user.
        _d.clear_state()
        print(f"agent-doctor: cleared corrupt state ({exc})", file=sys.stderr)
        print(json.dumps({"cancelled": True, "had_corrupt_state": True}, indent=2))
        return 0
    if state is None:
        print("no dictate recording in flight", file=sys.stderr)
        return 0
    try:
        audio = _d.stop_recording()
    except _d.DictateError as exc:
        _d.clear_state()
        print(f"agent-doctor: {exc}", file=sys.stderr)
        return 0
    try:
        Path(audio).unlink(missing_ok=True)
    finally:
        _d.clear_state()
    print(json.dumps({"cancelled": True}, indent=2))
    return 0


def _cmd_dictate_history(args: argparse.Namespace) -> int:
    from . import dictate as _d
    import datetime as _dt

    try:
        rows = _d.read_history(limit=args.limit)
    except _d.DictateError as exc:
        print(f"agent-doctor: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    elif not rows:
        print("(no dictate history yet)", file=sys.stderr)
    else:
        full = bool(args.full)
        def _truncate(s: str, n: int = 120) -> str:
            return s if full or len(s) <= n else s[: n - 1] + "\u2026"
        for row in rows:
            ts = _dt.datetime.fromtimestamp(row["ts"]).strftime("%Y-%m-%d %H:%M:%S")
            tag = "enhanced" if row["enhanced"] else ("raw" if row["mode"] == "raw" else "raw-fallback")
            failed = " (enhance-failed)" if row["enhancer_failed"] else ""
            print(f"#{row['id']:>3} {ts} [{row['mode']}/{tag}{failed}]")
            print(f"     transcript: {_truncate(row['transcript'])}")
            if row["prompt"] and row["prompt"] != row["transcript"]:
                print(f"     prompt:     {_truncate(row['prompt'])}")
            print()

    if args.clear:
        _d.clear_history()
        print("(history cleared)", file=sys.stderr)
    return 0


def _dictate_finish(args: argparse.Namespace) -> int:
    """Stop the recorder, transcribe once, optionally enhance, copy to clipboard.

    Error handling contract:
    - Recorder-side failures (no audio captured) abort with a non-zero rc; the
      audio file does not exist so nothing to clean up.
    - Transcription failures (whisper crash or empty text) abort with a non-zero
      rc; the WAV stays on disk so the user can debug, and state is cleared.
    - Enhancer failures only fall back to the raw transcript (the original
      goal of the graceful-degradation contract). Other errors propagate.
    - Clipboard write failures abort with a non-zero rc but still clean up
      audio + state.
    """

    from . import dictate as _d
    from . import pet_transient as _pt

    try:
        state = _d.read_state()
    except _d.DictateError as exc:
        print(f"agent-doctor: {exc}", file=sys.stderr)
        print("agent-doctor: run 'agent-doctor dictate cancel' to clear stale state", file=sys.stderr)
        return 2
    if state is None:
        print("agent-doctor: no dictate recording in flight", file=sys.stderr)
        return 2

    # Preserve the mode the recording was *started* in unless the user
    # explicitly overrode it on the stop/toggle invocation. argparse default
    # is None for --mode (see _add_common_dictate_args) so getattr returning
    # None means "user did not pass --mode".
    cli_mode = _maybe_deprecate_mode(getattr(args, "mode", None))
    mode = cli_mode if cli_mode is not None else _maybe_deprecate_mode(state.mode) or state.mode

    # Audio feedback (opt-in). The "done" / "fail" sounds fire later; play
    # the "stop chime" inline so the user hears that 'stop' was registered
    # even when the rest of the pipeline (transcribe + LLM) takes a few
    # seconds. Reusing the start sound is intentional: same audible
    # "registered" cue at both edges of the hotkey press.
    play_audio = _d.beep_enabled(getattr(args, "beep", None))
    if play_audio:
        _d.play_sound(_d.DEFAULT_START_SOUND)

    # Honour --buffer-ms BEFORE we send SIGTERM so the recorder captures
    # the user's final syllable (the hotkey is typically released slightly
    # before the user finishes the word).
    try:
        buffer_ms = _d.resolve_buffer_ms(getattr(args, "buffer_ms", None))
    except _d.DictateError as exc:
        print(f"agent-doctor: {exc}", file=sys.stderr)
        return 2
    _d.maybe_sleep_for_buffer(buffer_ms)

    t0 = time.time()
    with _pt.pet_state("listening", ttl_seconds=180.0):
        try:
            audio_path = _d.stop_recording()
        except _d.DictateError as exc:
            _d.clear_state()
            if play_audio:
                _d.play_sound(_d.DEFAULT_FAIL_SOUND)
            print(f"agent-doctor: {exc}", file=sys.stderr)
            return 2
        t_stop = time.time()

    keep_audio = bool(getattr(args, "keep_audio", False))
    audio_pathobj = Path(audio_path)
    enhance = not getattr(args, "no_enhance", False)
    from . import dictate_llm as _dl
    llm_config = _dl.llm_config(
        url=getattr(args, "llm_url", None),
        model=getattr(args, "llm_model", None),
        api_key=getattr(args, "llm_key", None),
    )
    # Resolve effective whisper model + backend so the history metadata
    # reflects what actually ran, not what the user happened to type. The
    # precedence (CLI > env > default) is the same order ``transcribe()``
    # uses internally; we compute it here too so ``record_history`` gets
    # the real values when neither CLI nor env was supplied.
    whisper_model = (
        getattr(args, "whisper_model", None)
        or os.environ.get(_d.ENV_WHISPER_MODEL)
        or _d.DEFAULT_WHISPER_MODEL
    )
    language = getattr(args, "language", None)
    backend_choice = (
        getattr(args, "backend", None)
        or os.environ.get(_d.ENV_BACKEND)
        or _d.DEFAULT_BACKEND
    )

    enhancer_failed_reason: Optional[str] = None
    result = None
    t_transcribe: Optional[float] = None
    t_enhance: Optional[float] = None

    try:
        with _pt.pet_state("listening", ttl_seconds=180.0):
            transcript = _d.transcribe(
                audio_pathobj,
                model_name=whisper_model,
                backend=backend_choice,
                language=language,
            )
            t_transcribe = time.time()
            if not transcript.strip():
                raise _d.DictateError("transcription produced no text; nothing to enhance")

        prompt = transcript
        enhanced = False
        if enhance and not _d.is_raw_mode(mode):
            with _pt.pet_state("thinking", ttl_seconds=60.0):
                try:
                    prompt = _d.enhance_prompt(
                        transcript,
                        mode=mode,
                        config=llm_config,
                    )
                    enhanced = bool(prompt)
                    if not enhanced:
                        prompt = transcript
                except _d.DictateError as exc:
                    enhancer_failed_reason = str(exc)
                    prompt = transcript
                    enhanced = False
        t_enhance = time.time()

        result = _d.DictateResult(
            transcript=transcript,
            prompt=prompt,
            mode=mode,
            audio_path=audio_pathobj,
            enhanced=enhanced,
        )

        try:
            _d.copy_to_clipboard(result.prompt)
        except _d.DictateError as exc:
            print(f"agent-doctor: {exc}", file=sys.stderr)
            if play_audio:
                _d.play_sound(_d.DEFAULT_FAIL_SOUND)
            return 2
        t_clipboard = time.time()

        _d.notify(
            "Dictate ready" if result.enhanced else "Dictate (raw)",
            f"{len(result.prompt)} chars on clipboard ({result.mode})",
        )

        if play_audio:
            _d.play_sound(_d.DEFAULT_DONE_SOUND)

        if not getattr(args, "no_history", False):
            try:
                _d.record_history(
                    transcript=result.transcript,
                    prompt=result.prompt,
                    mode=result.mode,
                    enhanced=result.enhanced,
                    enhancer_failed=enhancer_failed_reason is not None,
                    audio_path=result.audio_path if keep_audio else None,
                    backend=(
                        backend_choice
                        if backend_choice != "auto"
                        else _d.detect_backend(whisper_model)
                    ),
                    whisper_model=whisper_model,
                    language=language,
                )
            except _d.DictateError as exc:
                # Non-fatal: history is bookkeeping, do not fail the run.
                print(f"agent-doctor: warning: {exc}", file=sys.stderr)

        if enhancer_failed_reason:
            print(
                f"agent-doctor: enhancement failed, copied raw transcript ({enhancer_failed_reason})",
                file=sys.stderr,
            )
        if getattr(args, "print_transcript", False):
            print(result.transcript, file=sys.stderr)
        if getattr(args, "timing", False):
            print(
                json.dumps(
                    {
                        "stop_ms": round((t_stop - t0) * 1000),
                        "transcribe_ms": round(((t_transcribe or t_stop) - t_stop) * 1000),
                        "enhance_ms": round(((t_enhance or t_transcribe or t_stop) - (t_transcribe or t_stop)) * 1000),
                        "clipboard_ms": round((t_clipboard - (t_enhance or t_transcribe or t_stop)) * 1000),
                        "buffer_ms": buffer_ms,
                        "total_ms": round((t_clipboard - t0) * 1000 + buffer_ms),
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )

        print(
            json.dumps(
                {
                    "ok": True,
                    "enhanced": result.enhanced,
                    "mode": result.mode,
                    "prompt_chars": len(result.prompt),
                    "transcript_chars": len(result.transcript),
                    "enhancer_failed": enhancer_failed_reason is not None,
                },
                indent=2,
            )
        )
        return 0
    except _d.DictateError as exc:
        if play_audio:
            _d.play_sound(_d.DEFAULT_FAIL_SOUND)
        print(f"agent-doctor: {exc}", file=sys.stderr)
        return 2
    finally:
        _d.clear_state()
        if not keep_audio:
            try:
                audio_pathobj.unlink(missing_ok=True)
            except OSError:
                pass


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


def _cmd_dictate_models_list(args: argparse.Namespace) -> int:
    from . import dictate_models as _dm

    rows = _dm.list_status()
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    header = f"{'ID':<28} {'Status':<10} {'Size':>10}  Notes"
    print(header)
    print("-" * len(header))
    for row in rows:
        status = "installed" if row["installed"] else "available"
        size_mb = int(row["size_bytes"]) / (1024 * 1024)
        print(
            f"{row['id']:<28} {status:<10} {size_mb:>8.1f} MB  {row['display_name']}"
        )
    return 0


def _cmd_dictate_models_current(args: argparse.Namespace) -> int:
    from . import dictate_models as _dm

    cur = _dm.current()
    if getattr(args, "json", False):
        print(json.dumps(cur or {}, indent=2, sort_keys=True))
        return 0
    if cur is None:
        print("no transcription model selected")
        print("run 'agent-doctor dictate models list' and 'set' one")
        return 0
    print(f"{cur['id']}  ->  {cur['path']}")
    return 0


def _cmd_dictate_models_download(args: argparse.Namespace) -> int:
    from . import dictate_models as _dm

    try:
        path = _dm.download(args.model_id, force=args.force)
    except _dm.DictateModelsError as exc:
        print(f"agent-doctor: {exc}", file=sys.stderr)
        return 2
    print(f"installed: {path}")
    return 0


def _cmd_dictate_models_set(args: argparse.Namespace) -> int:
    from . import dictate_models as _dm

    try:
        path = _dm.set_active(args.model_id)
    except _dm.DictateModelsError as exc:
        print(f"agent-doctor: {exc}", file=sys.stderr)
        return 2
    print(f"active model: {args.model_id}  ->  {path}")
    return 0


def _cmd_dictate_models_remove(args: argparse.Namespace) -> int:
    from . import dictate_models as _dm

    try:
        removed = _dm.remove(args.model_id)
    except _dm.DictateModelsError as exc:
        print(f"agent-doctor: {exc}", file=sys.stderr)
        return 2
    print(f"removed: {args.model_id}" if removed else f"not installed: {args.model_id}")
    return 0


def _cmd_dictate_models_doctor(_args: argparse.Namespace) -> int:
    from . import dictate_models as _dm

    rows: list[dict[str, object]] = []
    rc = 0
    for entry in _dm.catalog():
        report = _dm.verify(entry.id)
        report["id"] = entry.id
        rows.append(report)
        if report["installed"] and not report["ok"]:
            rc = 2
    print(json.dumps(rows, indent=2, sort_keys=True))
    return rc


def _cmd_dictate_llm_probe(args: argparse.Namespace) -> int:
    from . import dictate_llm as _dl

    rows = _dl.probe_all()
    payload = [
        {
            "provider_id": r.provider_id,
            "base_url": r.base_url,
            "reachable": r.reachable,
            "models": r.models,
            "error": r.error,
        }
        for r in rows
    ]
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    for row in payload:
        status = "✓" if row["reachable"] else "✗"
        head = f"{status} {row['provider_id']:<10} {row['base_url']}"
        if row["reachable"]:
            print(f"{head}  ({len(row['models'])} model(s))")
            for mid in row["models"]:
                print(f"    - {mid}")
        else:
            print(f"{head}  unreachable: {row['error']}")
    return 0


def _cmd_dictate_llm_set(args: argparse.Namespace) -> int:
    from . import dictate_settings as _ds
    from . import dictate_llm as _dl

    try:
        provider = _dl.get_provider(args.provider)
    except _dl.DictateLLMError as exc:
        print(f"agent-doctor: {exc}", file=sys.stderr)
        return 2
    if args.url and not provider.allow_base_url_edit:
        print(
            f"agent-doctor: provider {provider.id!r} does not allow base-url override; "
            "use --provider custom to supply a URL",
            file=sys.stderr,
        )
        return 2
    settings = _ds.load()
    new_llm = _ds.LLMSettings(
        provider_id=provider.id,
        base_url=args.url or provider.base_url,
        model=args.model,
        api_key_ref=settings.llm.api_key_ref,
        timeout_s=settings.llm.timeout_s,
        optimize_prompt=settings.llm.optimize_prompt,
    )
    _ds.save(_ds.replace_section(settings, llm=new_llm))
    print(f"provider: {provider.id}\nurl: {new_llm.base_url}\nmodel: {new_llm.model or '(none)'}")
    return 0


def _cmd_dictate_llm_current(args: argparse.Namespace) -> int:
    from . import dictate_settings as _ds

    settings = _ds.load()
    payload = {
        "provider_id": settings.llm.provider_id,
        "base_url": settings.llm.base_url,
        "model": settings.llm.model,
        "timeout_s": settings.llm.timeout_s,
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(
        f"provider: {payload['provider_id']}\n"
        f"url: {payload['base_url']}\n"
        f"model: {payload['model'] or '(none)'}"
    )
    return 0


def _cmd_dictate_llm_test(args: argparse.Namespace) -> int:
    from . import dictate as _d
    from . import dictate_llm as _dl

    try:
        cfg = _dl.llm_config()
        result = _d.enhance_prompt(args.text, mode="optimize", config=cfg)
    except (_d.DictateError, _dl.DictateLLMError) as exc:
        print(f"agent-doctor: {exc}", file=sys.stderr)
        return 2
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
