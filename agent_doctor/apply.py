"""Stage reviewable patches from findings.

Apply mode is the bridge between "agent doctor diagnosed something" and "the
host agent's config actually changed". The MVP stopped at recommendations in a
report; reviewers had to translate proposals into edits by hand. ``apply``
takes findings.json and produces a staging directory of concrete patch files
(memory.md, sop.md, identity.md, eval cases) plus a unified diff against any
live target file.

The staging directory is the only thing that gets written. Real Hermes /
OpenClaw configuration is never touched: ``apply`` deliberately stops short of
the destructive step. A reviewer can copy-paste from staging into the live
config, or pipe the diff into ``patch``. That keeps the trust boundary
identical to the rest of Agent Doctor while giving humans something they can
actually act on.

The staging layout is::

    <staging>/
      memory.md          # one block per memory candidate finding
      sop.md             # SOP guidance, grouped by failure mode
      identity.md        # identity/communication-style guidance
      tool-discipline.md # tool-discipline guards
      eval/<id>.yaml     # one starter eval case per finding
      MANIFEST.json      # mapping from finding -> emitted patch files
      DIFF.txt           # unified diff vs target files (if --target given)
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .redaction import redact_text

PATCH_FILENAMES: dict[str, str] = {
    "memory": "memory.md",
    "sop": "sop.md",
    "identity": "identity.md",
    "tool_discipline": "tool-discipline.md",
}

TARGET_FILENAME_HINTS: dict[str, tuple[str, ...]] = {
    "memory": ("memory.md", "MEMORY.md", "memory.txt"),
    "sop": ("sop.md", "SOP.md", "operating-rules.md"),
    "identity": ("identity.md", "persona.md", "system.md"),
    "tool_discipline": ("tool-discipline.md", "tools.md"),
}

HEADERS: dict[str, str] = {
    "memory": "# Agent Doctor — Proposed Memory Candidates\n",
    "sop": "# Agent Doctor — Proposed SOP Patches\n",
    "identity": "# Agent Doctor — Proposed Identity Guidance\n",
    "tool_discipline": "# Agent Doctor — Proposed Tool Discipline Guards\n",
}


@dataclass(frozen=True)
class StageResult:
    """Result of staging patches into a directory."""

    staging_dir: Path
    files_written: list[Path]
    diff_text: str
    skipped: int


def stage_patches(
    findings: list[dict[str, Any]],
    out_dir: Path,
    *,
    target_dir: Path | None = None,
    minimum_severity: str = "low",
    minimum_count: int = 1,
) -> StageResult:
    """Materialize patch files for every actionable finding into ``out_dir``.

    Findings below ``minimum_severity`` or with ``count < minimum_count`` are
    skipped — useful for noise control on large transcripts. Existing files in
    ``out_dir`` are overwritten so re-runs are idempotent.
    """

    severity_threshold = _SEVERITY_RANK[minimum_severity]
    actionable: list[dict[str, Any]] = []
    skipped = 0
    for finding in findings:
        sev = finding.get("severity", "low")
        if _SEVERITY_RANK.get(sev, 0) < severity_threshold:
            skipped += 1
            continue
        if int(finding.get("count", 1)) < minimum_count:
            skipped += 1
            continue
        actionable.append(finding)

    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    blocks_by_target = _group_blocks_by_target(actionable)
    files_written: list[Path] = []
    manifest: dict[str, Any] = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "patches": {},
        "evals": [],
        "skipped_findings": skipped,
    }

    for target, blocks in blocks_by_target.items():
        filename = PATCH_FILENAMES.get(target)
        if not filename:
            continue
        path = out_dir / filename
        body = HEADERS.get(target, f"# {target}\n") + "\n".join(blocks) + "\n"
        _write_private_text(path, redact_text(body))
        files_written.append(path)
        manifest["patches"][target] = {
            "file": filename,
            "block_count": len(blocks),
        }

    eval_dir = out_dir / "eval"
    if any("eval_case" in finding for finding in actionable):
        eval_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for finding in actionable:
        case = finding.get("eval_case") or {}
        if not case:
            continue
        case_path = eval_dir / f"{finding['id']}.yaml"
        _write_private_text(case_path, redact_text(_render_eval_case(case, finding)))
        files_written.append(case_path)
        manifest["evals"].append({"finding_id": finding["id"], "file": str(case_path.relative_to(out_dir))})

    diff_text = ""
    if target_dir is not None:
        diff_text = _build_diff(out_dir, target_dir)
        diff_path = out_dir / "DIFF.txt"
        _write_private_text(diff_path, diff_text or "# No changes vs target.\n")
        files_written.append(diff_path)
        manifest["target_dir"] = str(target_dir)

    manifest_path = out_dir / "MANIFEST.json"
    _write_private_text(manifest_path, json.dumps(manifest, indent=2) + "\n")
    files_written.append(manifest_path)

    return StageResult(
        staging_dir=out_dir,
        files_written=files_written,
        diff_text=diff_text,
        skipped=skipped,
    )


def render_apply_summary(result: StageResult) -> str:
    lines = [
        f"Staged {len(result.files_written)} file(s) into {result.staging_dir}",
        f"Skipped {result.skipped} finding(s) below threshold." if result.skipped else "",
    ]
    if result.diff_text:
        lines.append("Diff vs target written to DIFF.txt — review before adopting.")
    else:
        lines.append("Run `agent-doctor apply --target <dir>` to also generate a diff.")
    lines.append(
        "Nothing was applied automatically. Copy patches into your agent config "
        "after review."
    )
    return "\n".join(line for line in lines if line)


def _group_blocks_by_target(findings: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    blocks: dict[str, list[str]] = {}
    for finding in findings:
        for recommendation in finding.get("recommendations", []) or []:
            target = recommendation.get("target")
            if not target or target not in PATCH_FILENAMES:
                continue
            blocks.setdefault(target, []).append(_render_block(finding, recommendation))
    return blocks


def _render_block(finding: dict[str, Any], recommendation: dict[str, Any]) -> str:
    proposal = recommendation.get("proposal", "").strip()
    quote = recommendation.get("evidence_quote", "").strip()
    evidence_lines = []
    for item in finding.get("evidence", []) or []:
        evidence_lines.append(
            f"  - `{item.get('file')}:{item.get('line')}` {item.get('role')}: \"{item.get('quote', '')}\""
        )
    evidence_block = "\n".join(evidence_lines) or "  - (no evidence)"
    severity = finding.get("severity", "?")
    count = finding.get("count", 1)
    return (
        f"\n## {finding.get('id')} — {finding.get('title')}\n"
        f"\n- Severity: {severity}"
        f"\n- Occurrences: {count}"
        f"\n- Session: `{finding.get('session_id', '')}`"
        f"\n\nProposal: {proposal}\n"
        f"\nEvidence:\n{evidence_block}\n"
        + (f"\nRepresentative quote: \"{quote}\"\n" if quote else "")
    )


def _render_eval_case(case: dict[str, Any], finding: dict[str, Any]) -> str:
    return (
        f"id: {json.dumps(finding.get('id', ''))}\n"
        f"failure_mode: {json.dumps(finding.get('failure_mode', ''))}\n"
        f"name: {json.dumps(case.get('name', ''))}\n"
        "prompt: |-\n"
        f"  {case.get('prompt', '').strip()}\n"
        "expected_behavior: |-\n"
        f"  {case.get('expected_behavior', '').strip()}\n"
    )


def _build_diff(staging_dir: Path, target_dir: Path) -> str:
    """Return a unified diff between the staged patches and any matching files
    in ``target_dir``.

    For each emitted patch file, look for a likely live counterpart by name;
    if none exists, the diff is computed against an empty file (so reviewers
    see the entire proposed addition).
    """

    diffs: list[str] = []
    for target_kind, filename in PATCH_FILENAMES.items():
        staged_path = staging_dir / filename
        if not staged_path.exists():
            continue
        live_path = _find_live_counterpart(target_dir, target_kind)
        live_text = live_path.read_text(encoding="utf-8") if live_path and live_path.exists() else ""
        staged_text = staged_path.read_text(encoding="utf-8")
        if live_text == staged_text:
            continue
        diff = difflib.unified_diff(
            live_text.splitlines(),
            staged_text.splitlines(),
            fromfile=str(live_path) if live_path else f"<missing>/{filename}",
            tofile=str(staged_path),
            lineterm="",
        )
        diffs.append("\n".join(diff))
    return "\n\n".join(diff for diff in diffs if diff)


def _find_live_counterpart(target_dir: Path, target_kind: str) -> Path | None:
    candidates = TARGET_FILENAME_HINTS.get(target_kind, ())
    for name in candidates:
        candidate = target_dir / name
        if candidate.exists():
            return candidate
    # Fall back to first hint so the diff renders the patch as a brand-new file.
    if candidates:
        return target_dir / candidates[0]
    return None


_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}


def _write_private_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    finally:
        os.chmod(path, 0o600)


def load_findings(path: Path) -> list[dict[str, Any]]:
    """Load findings.json, accepting either a list or ``{"findings": [...]}``."""

    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict) and isinstance(raw.get("findings"), list):
        return raw["findings"]
    raise ValueError(f"{path} does not contain a findings list.")
