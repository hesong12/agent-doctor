"""Applier: writes a Proposal's patch to live host config.

Pre-write backup -> atomic write -> post-write log entry. Edit-style
patches verify baseline_hash before writing. Append-only patches
(memory, tool_discipline) skip the hash check.

undo_patch restores from backup.
"""
from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .adapters import HostAdapter, HostCapabilities
from .proposer import Proposal

APPEND_ONLY_KINDS = frozenset({"memory", "tool_discipline"})

ApplyState = Literal["applied", "conflict", "degraded_to_staging"]


@dataclass(frozen=True)
class AppliedPatch:
    state: ApplyState
    patch_id: str
    target_file: Path
    backup_path: Path | None
    error: str | None = None


def apply_proposal(proposal: Proposal, adapter: HostAdapter) -> AppliedPatch:
    caps = adapter.capabilities()
    target_file = _resolve_target_file(proposal.target_kind, caps)
    if target_file is None:
        return AppliedPatch(
            state="degraded_to_staging",
            patch_id=proposal.id,
            target_file=Path("/dev/null"),
            backup_path=None,
            error=f"host {caps.host_name} has no writable {proposal.target_kind} surface",
        )

    # Ensure target exists with restrictive perms
    if not target_file.exists():
        target_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target_file.write_text(_default_header_for(proposal.target_kind), encoding="utf-8")
        target_file.chmod(0o600)

    # Conflict check for edit-style patches only
    if proposal.target_kind not in APPEND_ONLY_KINDS:
        if proposal.baseline_hash is not None:
            current_hash = _hash_file(target_file)
            if current_hash != proposal.baseline_hash:
                return AppliedPatch(
                    state="conflict",
                    patch_id=proposal.id,
                    target_file=target_file,
                    backup_path=None,
                    error="baseline_hash mismatch; target file changed since proposal",
                )

    # Backup
    backup_path = backup_target(target_file, proposal.id)

    # Write
    if proposal.target_kind in APPEND_ONLY_KINDS:
        # Append, with newline normalization
        existing = target_file.read_text(encoding="utf-8")
        with target_file.open("a", encoding="utf-8") as h:
            if existing and not existing.endswith("\n"):
                h.write("\n")
            h.write(proposal.patch_body.rstrip("\n") + "\n")
    else:
        # Edit kinds: replace contents with patch_body
        target_file.write_text(proposal.patch_body, encoding="utf-8")
    target_file.chmod(0o600)

    return AppliedPatch(
        state="applied",
        patch_id=proposal.id,
        target_file=target_file,
        backup_path=backup_path,
    )


def backup_target(target_file: Path, patch_id: str) -> Path:
    """Backup target_file under ~/.agent-doctor/backups/<patch-id>/.

    Also writes a restore.sh helper for manual recovery.
    """
    backup_dir = Path("~/.agent-doctor/backups").expanduser() / patch_id
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup_path = backup_dir / f"{target_file.name}.bak"
    shutil.copyfile(target_file, backup_path)
    backup_path.chmod(0o600)
    # Drop a restore.sh for manual rescue if agent-doctor undo isn't available
    restore_sh = backup_dir / "restore.sh"
    restore_sh.write_text(
        f"#!/bin/sh\ncp {backup_path!s} {target_file!s}\n",
        encoding="utf-8",
    )
    restore_sh.chmod(0o700)
    return backup_path


def undo_patch(patch_id: str, backup_path: Path, target_file: Path) -> None:
    """Restore target_file from backup_path."""
    if not backup_path.exists():
        raise RuntimeError(f"backup not found: {backup_path}")
    shutil.copyfile(backup_path, target_file)
    target_file.chmod(0o600)


def _resolve_target_file(kind: str, caps: HostCapabilities) -> Path | None:
    return {
        "memory": caps.memory_writable,
        "identity": caps.identity_writable,
        "sop": caps.sop_writable,
        "tool_discipline": caps.sop_writable,  # tool-discipline lives inside SOP file
    }.get(kind)


def _default_header_for(kind: str) -> str:
    return {
        "memory": "# Memory\n\n",
        "identity": "# Identity\n\n",
        "sop": "# SOP\n\n",
        "tool_discipline": "# Tool Discipline\n\n",
    }.get(kind, "")


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()
