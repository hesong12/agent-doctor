"""LLM tab logic (provider, base_url, model, optimize prompt, gemini reuse)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from agent_doctor import dictate_llm as dl
from agent_doctor import dictate_settings as ds


class LLMStateError(ValueError):
    pass


@dataclass
class LLMState:
    provider_id: str
    base_url: str
    model: Optional[str]
    api_key: Optional[str]
    timeout_s: int
    optimize_prompt: Optional[str]
    reuse_gemini_key: bool = False

    @classmethod
    def from_settings(cls) -> "LLMState":
        s = ds.load()
        return cls(
            provider_id=s.llm.provider_id,
            base_url=s.llm.base_url,
            model=s.llm.model,
            api_key=None,
            timeout_s=s.llm.timeout_s,
            optimize_prompt=s.llm.optimize_prompt,
            reuse_gemini_key=s.llm.reuse_gemini_key,
        )

    def apply(self) -> None:
        provider = dl.get_provider(self.provider_id)
        if not provider.allow_base_url_edit and self.base_url != provider.base_url:
            raise LLMStateError(
                f"provider {self.provider_id!r} requires base_url {provider.base_url!r}; "
                "switch to 'custom' to override"
            )
        if self.timeout_s <= 0 or self.timeout_s > 600:
            raise LLMStateError(f"timeout_s must be 1..600 (got {self.timeout_s})")
        s = ds.load()
        new = ds.LLMSettings(
            provider_id=self.provider_id,
            base_url=self.base_url,
            model=self.model,
            api_key_ref=s.llm.api_key_ref,
            timeout_s=int(self.timeout_s),
            optimize_prompt=self.optimize_prompt,
            reuse_gemini_key=bool(self.reuse_gemini_key),
        )
        ds.save(ds.replace_section(s, llm=new))


def probe_providers(timeout: float = 5.0) -> List[dl.ProbeResult]:
    return dl.probe_all(timeout=timeout)


def probe_one(provider_id: str, *, timeout: float = 5.0) -> dl.ProbeResult:
    """Probe a single provider, resolving the API key the same way ``llm_config`` does.

    Used by the "Test connection" button in the LLM tab so the gemini provider
    (and the reuse-checkbox path) gets the stored Gemini key as a Bearer token
    instead of always probing anonymously.
    """

    from agent_doctor import settings as gs

    s = ds.load()
    provider = dl.get_provider(provider_id)
    api_key: Optional[str] = None
    if provider_id == "gemini" or s.llm.reuse_gemini_key:
        api_key = gs.load_gemini_key()
    result = dl.probe(
        provider.base_url,
        provider.models_endpoint,
        timeout=timeout,
        api_key=api_key,
    )
    return dl.ProbeResult(
        provider_id=provider_id,
        base_url=result.base_url,
        reachable=result.reachable,
        models=result.models,
        error=result.error,
    )


def looks_like_gemini_model(model_id: Optional[str]) -> bool:
    """Heuristic: True if ``model_id`` looks like a Gemini text-model id.

    Used by the LLM tab to decide whether to keep or clear the current model
    selection when the user switches the provider between gemini and a
    non-gemini provider. We treat both bare ``gemini-…`` and the API-style
    ``models/gemini-…`` as valid since Gemini's OpenAI-compatible ``/models``
    endpoint returns the latter form.
    """

    if not model_id:
        return False
    m = model_id.strip().lower()
    return m.startswith("gemini-") or m.startswith("models/gemini-")


def list_gemini_models(*, timeout: float = 3.0) -> List[str]:
    """Return Gemini's available text models, or an empty list on failure.

    Wraps :func:`probe_one` for the ``gemini`` provider so the LLM tab can
    populate its Model combobox dynamically. Failures (no key configured,
    network unreachable, 401 from upstream) collapse to ``[]`` — the caller
    decides how to render an empty dropdown.
    """

    result = probe_one("gemini", timeout=timeout)
    if not result.reachable:
        return []
    return list(result.models)


def fetch_models_for(provider_id: str, base_url: Optional[str] = None, *, timeout: float = 5.0):
    p = dl.get_provider(provider_id)
    url = base_url or p.base_url
    return dl.probe(url, p.models_endpoint, timeout=timeout)
