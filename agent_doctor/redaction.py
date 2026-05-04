"""Redaction helpers for transcript-derived report content."""

from __future__ import annotations

import re
from typing import Any

SECRET_ASSIGNMENT = re.compile(
    r"\b("
    r"api[_-]?key|"
    r"access[_-]?token|"
    r"refresh[_-]?token|"
    r"client[_-]?secret|"
    r"auth[_-]?token|"
    r"authorization|"
    r"password|"
    r"secret|"
    r"token"
    r")\b([\"']?\s*[:=]\s*[\"']?)((?:Bearer\s+)?[^\"'\s,;}]{6,})",
    re.IGNORECASE,
)
BEARER_TOKEN = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
GITHUB_TOKEN = re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{12,}\b")
GITHUB_FINE_GRAINED_TOKEN = re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}\b")


def redact_text(text: str) -> str:
    """Return text with common secrets and bearer tokens replaced."""

    redacted = BEARER_TOKEN.sub("Bearer [REDACTED]", text)
    redacted = SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", redacted)
    redacted = OPENAI_KEY.sub("sk-[REDACTED]", redacted)
    redacted = GITHUB_TOKEN.sub("[REDACTED_GITHUB_TOKEN]", redacted)
    redacted = GITHUB_FINE_GRAINED_TOKEN.sub("[REDACTED_GITHUB_TOKEN]", redacted)
    redacted = JWT.sub("[REDACTED_JWT]", redacted)
    return redacted


def redact_value(value: Any) -> Any:
    """Recursively redact strings in JSON-serializable report values."""

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    return value
