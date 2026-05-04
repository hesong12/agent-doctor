"""Generate safe Agent Doctor skill/SOP files."""

from __future__ import annotations

from pathlib import Path


VALID_TARGETS = {"hermes", "openclaw"}


def install_skill(target: str, out_dir: Path) -> Path:
    normalized = target.strip().casefold()
    if normalized not in VALID_TARGETS:
        raise ValueError(f"Unsupported target: {target}. Expected hermes or openclaw.")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"agent-doctor-{normalized}-sop.md"
    path.write_text(_skill_text(normalized), encoding="utf-8")
    return path


def _skill_text(target: str) -> str:
    label = "Hermes" if target == "hermes" else "OpenClaw"
    default_flag = "--hermes" if target == "hermes" else "--openclaw"
    return f"""# Agent Doctor SOP for {label}

Use Agent Doctor as a local-first engineering diagnosis tool for agent session postmortems.

## Operating Rules

- Run Agent Doctor local-only with `agent-doctor scan {default_flag} --format markdown --out <output-dir>`.
- Treat all output as dry-run by default. Review proposed patches before adopting them.
- Ask before applying patches to memory, identity, skills, SOPs, permissions, routing, evals, or config.
- Never paste full transcripts to a remote LLM unless the user explicitly approves that disclosure.
- Prefer evidence quotes and line spans over broad claims about the user or agent.
- Do not use Agent Doctor for therapy, HR performance management, or surveillance analytics.

## Review Loop

1. Scan the target transcript directory.
2. Read `report.md`, `findings.json`, and `eval-cases.yaml`.
3. Convert accepted recommendations into explicit memory, SOP, skill, or eval changes.
4. Keep rejected recommendations as non-applied review notes.
"""
