"""Scenario cards: the seed for synthetic transcript generation.

A scenario card declares everything the generator needs to produce a
ground-truth-labeled transcript:

- the task and personas to roleplay,
- which failure modes must be present, at which turn, and at what severity,
- which distractors must be present (false-positive bait),
- the output format (``hermes``, ``openclaw``, or ``generic``).

We avoid PyYAML to keep the runtime dependency-free, and instead support a
constrained subset of YAML — enough for our cards. Cards can also be JSON,
which is always parseable.

The validator is strict on purpose. A weakly-typed card slips through and
silently breaks the benchmark numbers; we'd rather refuse to load a card with
an unknown failure mode than produce noisy P/R/F1.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VALID_FAILURE_MODES: set[str] = {
    "repeated_user_correction",
    "execution_discipline",
    "verification_failure",
    "memory_failure",
    "tool_failure_or_hidden_error",
    "communication_mismatch",
}

VALID_SEVERITIES: set[str] = {"low", "medium", "high"}
VALID_FORMATS: set[str] = {"hermes", "openclaw", "generic"}
VALID_DISTRACTORS: set[str] = {
    "remember_in_neutral_context",
    "zero_errors_in_tool",
    "i_can_offer",
    "no_problem_filler",
}


@dataclass(frozen=True)
class SeededFailure:
    mode: str
    turn: int
    severity: str = "medium"


@dataclass(frozen=True)
class Distractor:
    kind: str
    turn: int


@dataclass(frozen=True)
class ScenarioCard:
    id: str
    task: str
    agent_persona: str
    user_persona: str
    length_turns: int
    output_format: str
    seeded_failures: list[SeededFailure] = field(default_factory=list)
    distractors: list[Distractor] = field(default_factory=list)


class CardError(ValueError):
    """Raised when a scenario card fails validation."""


def load_card(path: Path) -> ScenarioCard:
    raw = _parse(path)
    return _validate(raw, source=str(path))


def load_cards(path: Path) -> list[ScenarioCard]:
    expanded = path.expanduser()
    if not expanded.exists():
        raise CardError(f"Card path does not exist: {expanded}")
    if expanded.is_file():
        return [load_card(expanded)]
    cards: list[ScenarioCard] = []
    for candidate in sorted(expanded.iterdir()):
        if candidate.is_file() and candidate.suffix in {".yaml", ".yml", ".json"}:
            cards.append(load_card(candidate))
    if not cards:
        raise CardError(f"No card files found under {expanded}")
    return cards


def _parse(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise CardError(f"{path}: invalid JSON: {exc}") from exc
    return _parse_simple_yaml(text, source=str(path))


def _validate(raw: dict[str, Any], *, source: str) -> ScenarioCard:
    if not isinstance(raw, dict):
        raise CardError(f"{source}: card must be a mapping at top level")
    required = {"id", "task", "agent_persona", "user_persona", "length_turns", "output_format"}
    missing = required - raw.keys()
    if missing:
        raise CardError(f"{source}: missing required fields {sorted(missing)}")

    output_format = str(raw["output_format"]).strip()
    if output_format not in VALID_FORMATS:
        raise CardError(
            f"{source}: output_format must be one of {sorted(VALID_FORMATS)}, got {output_format!r}"
        )

    length = int(raw["length_turns"])
    if length < 2:
        raise CardError(f"{source}: length_turns must be >= 2")

    seeded: list[SeededFailure] = []
    for entry in raw.get("seeded_failures", []) or []:
        if not isinstance(entry, dict):
            raise CardError(f"{source}: seeded_failures entries must be mappings")
        mode = str(entry.get("mode", "")).strip()
        if mode not in VALID_FAILURE_MODES:
            raise CardError(
                f"{source}: unknown failure mode {mode!r}; expected one of {sorted(VALID_FAILURE_MODES)}"
            )
        turn = int(entry.get("turn", 0))
        if turn < 1 or turn > length:
            raise CardError(f"{source}: failure turn {turn} out of range 1..{length}")
        severity = str(entry.get("severity", "medium")).strip()
        if severity not in VALID_SEVERITIES:
            raise CardError(f"{source}: severity must be one of {sorted(VALID_SEVERITIES)}")
        seeded.append(SeededFailure(mode=mode, turn=turn, severity=severity))

    distractors: list[Distractor] = []
    for entry in raw.get("distractors", []) or []:
        if not isinstance(entry, dict):
            raise CardError(f"{source}: distractors entries must be mappings")
        kind = str(entry.get("kind", "")).strip()
        if kind not in VALID_DISTRACTORS:
            raise CardError(
                f"{source}: unknown distractor {kind!r}; expected one of {sorted(VALID_DISTRACTORS)}"
            )
        turn = int(entry.get("turn", 0))
        if turn < 1 or turn > length:
            raise CardError(f"{source}: distractor turn {turn} out of range 1..{length}")
        distractors.append(Distractor(kind=kind, turn=turn))

    return ScenarioCard(
        id=str(raw["id"]).strip(),
        task=str(raw["task"]).strip(),
        agent_persona=str(raw["agent_persona"]).strip(),
        user_persona=str(raw["user_persona"]).strip(),
        length_turns=length,
        output_format=output_format,
        seeded_failures=seeded,
        distractors=distractors,
    )


# ---------------------------------------------------------------------------
# Minimal YAML subset parser.
#
# We only need to support the shape used by scenario cards:
#
#   key: value
#   key: |-     (multi-line block scalars)
#   key:
#     - mapping_inline
#     - key: value
#       key: value
#   # comments and blank lines
#
# This is far short of a real YAML parser, but it's enough to keep us
# dependency-free for the eval harness while still rejecting malformed input.
# ---------------------------------------------------------------------------


def _parse_simple_yaml(text: str, *, source: str) -> dict[str, Any]:
    parser = _SimpleYamlParser(text, source)
    return parser.parse()


class _SimpleYamlParser:
    def __init__(self, text: str, source: str):
        self.lines = [line.rstrip("\n") for line in text.splitlines()]
        self.source = source
        self.i = 0

    def parse(self) -> dict[str, Any]:
        result, _ = self._parse_mapping(0)
        return result

    def _peek(self) -> tuple[int, str] | None:
        while self.i < len(self.lines):
            line = self.lines[self.i]
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                self.i += 1
                continue
            indent = len(line) - len(line.lstrip(" "))
            return indent, line.lstrip(" ").rstrip()
        return None

    def _parse_mapping(self, indent: int) -> tuple[dict[str, Any], int]:
        result: dict[str, Any] = {}
        while True:
            peek = self._peek()
            if peek is None:
                return result, self.i
            current_indent, content = peek
            if current_indent < indent:
                return result, self.i
            if current_indent > indent:
                raise CardError(f"{self.source}: unexpected indent at line {self.i + 1}")
            if ":" not in content:
                raise CardError(f"{self.source}: expected mapping at line {self.i + 1}: {content!r}")
            key, _, raw_value = content.partition(":")
            key = key.strip()
            raw_value = raw_value.strip()
            self.i += 1
            if raw_value == "|-" or raw_value == "|":
                result[key] = self._parse_block_scalar(indent + 2)
            elif raw_value == "":
                next_peek = self._peek()
                if next_peek is None or next_peek[0] <= indent:
                    result[key] = None
                    continue
                child_indent = next_peek[0]
                if next_peek[1].startswith("- "):
                    result[key] = self._parse_sequence(child_indent)
                else:
                    nested, _ = self._parse_mapping(child_indent)
                    result[key] = nested
            else:
                result[key] = _coerce_scalar(raw_value)

    def _parse_sequence(self, indent: int) -> list[Any]:
        items: list[Any] = []
        while True:
            peek = self._peek()
            if peek is None or peek[0] != indent or not peek[1].startswith("- "):
                return items
            _, content = peek
            inner = content[2:].strip()
            self.i += 1
            if ":" in inner:
                first_key, _, first_value = inner.partition(":")
                mapping: dict[str, Any] = {first_key.strip(): _coerce_scalar(first_value.strip())}
                more, _ = self._parse_mapping(indent + 2)
                mapping.update(more)
                items.append(mapping)
            else:
                items.append(_coerce_scalar(inner))

    def _parse_block_scalar(self, indent: int) -> str:
        collected: list[str] = []
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if line.strip() == "":
                collected.append("")
                self.i += 1
                continue
            current_indent = len(line) - len(line.lstrip(" "))
            if current_indent < indent:
                break
            collected.append(line[indent:])
            self.i += 1
        return "\n".join(collected).rstrip()


def _coerce_scalar(value: str) -> Any:
    if value == "" or value.lower() == "null":
        return None
    if value == "[]":
        return []
    if value == "{}":
        return {}
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value
