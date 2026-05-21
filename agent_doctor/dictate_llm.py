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
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from agent_doctor.dictate import (
    ENV_LLM_KEY,
    ENV_LLM_MODEL,
    ENV_LLM_URL,
    LLMConfig,
)


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
    Provider(
        id="gemini",
        label="Gemini (OpenAI-compatible)",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        models_endpoint="/models",
        requires_api_key=True,
        allow_base_url_edit=False,
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


@dataclass(frozen=True)
class ProbeResult:
    provider_id: str
    base_url: str
    reachable: bool
    models: list[str]
    error: Optional[str] = None


def probe(
    base_url: str,
    models_endpoint: str,
    *,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    api_key: Optional[str] = None,
) -> ProbeResult:
    """Issue ``GET <base_url><models_endpoint>`` and return a ProbeResult.

    Never raises on network errors; failures are reported via ``reachable=False``
    and the ``error`` field. The OpenAI-style response shape is::

        {"object": "list", "data": [{"id": "name", ...}, ...]}

    We extract the ``id`` fields and discard the rest.
    """

    url = base_url.rstrip("/") + models_endpoint
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    context = None
    if url.lower().startswith("https"):
        from agent_doctor._https import make_https_context
        context = make_https_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return ProbeResult(
            provider_id="",
            base_url=base_url,
            reachable=False,
            models=[],
            error=f"HTTP {exc.code} {exc.reason}",
        )
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        return ProbeResult(
            provider_id="",
            base_url=base_url,
            reachable=False,
            models=[],
            error=str(exc),
        )

    try:
        parsed = json.loads(raw)
        data = parsed.get("data") if isinstance(parsed, dict) else None
        if not isinstance(data, list):
            return ProbeResult(
                provider_id="",
                base_url=base_url,
                reachable=False,
                models=[],
                error=f"unexpected response shape: {raw[:120]}",
            )
        models: list[str] = []
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                models.append(item["id"])
        return ProbeResult(
            provider_id="",
            base_url=base_url,
            reachable=True,
            models=models,
            error=None,
        )
    except json.JSONDecodeError as exc:
        return ProbeResult(
            provider_id="",
            base_url=base_url,
            reachable=False,
            models=[],
            error=f"invalid JSON: {exc.msg}",
        )


def probe_all(*, timeout: float = PROBE_TIMEOUT_SECONDS) -> list[ProbeResult]:
    """Probe every catalog provider. Returns one ProbeResult per provider."""

    out: list[ProbeResult] = []
    for prov in _PROVIDERS:
        result = probe(prov.base_url, prov.models_endpoint, timeout=timeout)
        out.append(
            ProbeResult(
                provider_id=prov.id,
                base_url=result.base_url,
                reachable=result.reachable,
                models=result.models,
                error=result.error,
            )
        )
    return out


def llm_config(
    *,
    url: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
) -> LLMConfig:
    """Resolve an :class:`LLMConfig` with this precedence order:

    1. Explicit kwargs (CLI flags)
    2. Environment variables
    3. ``~/.agent-doctor/dictate.json`` settings
    4. Provider-default base URL

    The settings ``base_url`` is OpenAI-style root (``.../v1``) - we append
    ``/chat/completions`` so the existing ``_default_llm_call`` continues to
    work. The legacy env var keeps its historical literal value so users with
    pre-existing config are unaffected.
    """

    from . import dictate_settings as _ds  # local import to avoid cycle

    settings = _ds.load()
    provider = get_provider(settings.llm.provider_id)
    settings_url = (
        (settings.llm.base_url or provider.base_url).rstrip("/") + "/chat/completions"
    )
    settings_model = settings.llm.model

    resolved_url = url or os.environ.get(ENV_LLM_URL) or settings_url
    resolved_model = (
        model or os.environ.get(ENV_LLM_MODEL) or settings_model or "default"
    )

    # api_key precedence: explicit kwarg > env > gemini-reuse fallback > None.
    # The gemini-reuse fallback fires when the user picked the gemini provider
    # (its endpoint requires a key) or ticked the "reuse Gemini API key"
    # checkbox while on another provider. Local import keeps the
    # dictate_llm <-> settings module pair acyclic.
    resolved_key: Optional[str] = api_key or os.environ.get(ENV_LLM_KEY)
    if resolved_key is None and (
        settings.llm.provider_id == "gemini" or settings.llm.reuse_gemini_key
    ):
        from . import settings as _gs
        resolved_key = _gs.load_gemini_key()

    return LLMConfig(
        url=resolved_url,
        model=resolved_model,
        api_key=resolved_key,
        timeout=float(settings.llm.timeout_s),
    )
