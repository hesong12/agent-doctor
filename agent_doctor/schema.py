"""Shared data structures for Agent Doctor."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Role = Literal["user", "assistant", "tool", "system/metadata"]
Severity = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class Message:
    file: str
    line: int
    session_id: str
    role: Role
    content: str
    source_format: str = "generic"
    raw_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Evidence:
    file: str
    line: int
    role: Role
    quote: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Finding:
    id: str
    severity: Severity
    failure_mode: str
    title: str
    evidence: list[Evidence]
    diagnosis: str
    recommendations: list[dict[str, str]]
    eval_case: dict[str, str]
    confidence: float
    session_id: str = ""
    count: int = 1

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence"] = [item.to_dict() for item in self.evidence]
        return data


@dataclass(frozen=True)
class ScanResult:
    messages: list[Message] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    parse_errors: int = 0

    @property
    def session_count(self) -> int:
        return len({message.session_id for message in self.messages})
