"""OpenAI-compatible LLM provider catalog + probe helpers for dictate.

Three providers ship in the catalog:

- ``lm_studio``  -> http://localhost:1234/v1  (LM Studio's OpenAI-compatible server)
- ``ollama``     -> http://localhost:11434/v1 (Ollama's OpenAI-compatible server)
- ``custom``     -> user-supplied URL, optional API key, free-form

Probing a provider issues ``GET <base_url>/models`` with a short timeout and
returns the parsed model list. We never store API keys in this module - they
live in the system keychain via :mod:`agent_doctor.settings`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional


PROBE_TIMEOUT_SECONDS = 10.0


class DictateLLMError(RuntimeError):
    """Raised on unknown provider, probe failure, or invalid response."""


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    base_url: str
    models_endpoint: str
    requires_api_key: bool
    allow_base_url_edit: bool


_PROVIDERS: tuple[Provider, ...] = (
    Provider(
        id="lm_studio",
        label="LM Studio (local)",
        base_url="http://localhost:1234/v1",
        models_endpoint="/models",
        requires_api_key=False,
        allow_base_url_edit=False,
    ),
    Provider(
        id="ollama",
        label="Ollama (local)",
        base_url="http://localhost:11434/v1",
        models_endpoint="/models",
        requires_api_key=False,
        allow_base_url_edit=False,
    ),
    Provider(
        id="custom",
        label="Custom (OpenAI-compatible)",
        base_url="http://localhost:8080/v1",
        models_endpoint="/models",
        requires_api_key=False,
        allow_base_url_edit=True,
    ),
)


def providers() -> tuple[Provider, ...]:
    return _PROVIDERS


def get_provider(provider_id: str) -> Provider:
    for p in _PROVIDERS:
        if p.id == provider_id:
            return p
    raise DictateLLMError(
        f"unknown provider {provider_id!r}; expected one of: "
        f"{', '.join(p.id for p in _PROVIDERS)}"
    )
