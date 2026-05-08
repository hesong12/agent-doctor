"""Per-user frustration dictionary persisted to ~/.agent-doctor/<host>/user-dict.json.

Fed by reaction watcher (Phase 4):
- ❌ on a 🩺 detection → trigger phrase joins `negative` list (do not flag this again).
- ✅ on a propose message that referenced a previously-undetected pattern →
  trigger phrase joins `positive` list (do flag).

The dict is consulted by the classifier alongside the regex tier:
- A message containing a `negative` phrase has its score reduced.
- A message containing a `positive` phrase has its score boosted.

This is per-host because each host's user has different vocabulary.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class UserDict:
    """Per-user phrase memory.

    Mutable so reaction watcher can append phrases. Persisted via
    save_user_dict(path, user_dict).
    """
    positive: list[str] = field(default_factory=list)
    negative: list[str] = field(default_factory=list)

    def add_positive(self, phrase: str) -> None:
        phrase = phrase.strip()
        if phrase and phrase not in self.positive:
            self.positive.append(phrase)

    def add_negative(self, phrase: str) -> None:
        phrase = phrase.strip()
        if phrase and phrase not in self.negative:
            self.negative.append(phrase)

    def score_adjustment(self, text: str) -> int:
        """+1 per positive match, -1 per negative match (capped to ±2)."""
        text_lc = text.lower()
        adjust = 0
        for p in self.positive:
            if p.lower() in text_lc:
                adjust += 1
        for n in self.negative:
            if n.lower() in text_lc:
                adjust -= 1
        return max(-2, min(2, adjust))


def load_user_dict(path: Path) -> UserDict:
    """Load the dict, returning an empty one if file is missing/malformed."""
    path = path.expanduser()
    if not path.exists():
        return UserDict()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return UserDict(
            positive=list(data.get("positive", [])),
            negative=list(data.get("negative", [])),
        )
    except (json.JSONDecodeError, OSError):
        return UserDict()


def save_user_dict(path: Path, user_dict: UserDict) -> None:
    """Atomic write with 0o600."""
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps(asdict(user_dict), ensure_ascii=False, indent=2)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as h:
            h.write(payload + "\n")
    finally:
        os.chmod(path, 0o600)
