# Dictate Phase 2 — Optimize prompt + LM Studio / Ollama / Custom Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the four mode prompts (`chat`/`coding`/`research`/`raw`) into a single "optimize for any downstream LLM" prompt, add a provider catalog (LM Studio / Ollama / Custom) with model discovery, and ship `agent-doctor dictate llm {probe,set,current,test}`.

**Architecture:** New `dictate_llm.py` owns the provider catalog and the `/v1/models` probe. `mode_system_prompt()` in `dictate.py` is reduced to returning a single `OPTIMIZE_PROMPT` (with optional settings-driven override) for any non-`raw` mode; deprecated mode flags emit a stderr warning. `llm_config_from_env()` is replaced by `llm_config()` which consults settings, env, and CLI overrides in that order.

**Tech Stack:** Python stdlib (urllib for the probe + provider call, json, argparse). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-05-14-dictate-handy-parity-design.md` §6.

**Prereq:** Phase 1 landed (`dictate_settings.py` is available).

---

## File Structure

| File | Responsibility |
| --- | --- |
| `agent_doctor/dictate_llm.py` | Create — Provider dataclass, hard-coded catalog, `probe()`, settings-driven `llm_config()`. |
| `agent_doctor/dictate.py` | Modify — Replace `_MODE_PROMPTS` with `OPTIMIZE_PROMPT`; route every non-raw mode through it; deprecation warning when old mode arrives via CLI. |
| `agent_doctor/cli.py` | Modify — Register `dictate llm {probe,set,current,test}`; emit deprecation warning in `_add_common_dictate_args` when `--mode chat/coding/research` is supplied. |
| `tests/test_dictate_llm.py` | Create — Provider catalog, fake HTTP server probe matrix, settings round-trip. |
| `tests/test_dictate.py` | Modify — Existing mode-prompt tests updated; new optimize-prompt + settings-override tests. |
| `tests/test_cli_subcommand_registration.py` | Modify — Add `dictate llm` subcommands to the assertion. |

---

## Task 1: Create `dictate_llm.py` with the provider catalog

**Files:**
- Create: `agent_doctor/dictate_llm.py`
- Test: `tests/test_dictate_llm.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_dictate_llm.py`:

```python
"""Tests for the LLM provider catalog and probe helpers."""

from __future__ import annotations

import http.server
import json
import socket
import threading
from pathlib import Path
from typing import Iterator

import pytest

from agent_doctor import dictate_llm as dl


def test_catalog_has_three_known_providers() -> None:
    providers = dl.providers()
    ids = {p.id for p in providers}
    assert ids == {"lm_studio", "ollama", "custom"}


def test_get_returns_provider_by_id() -> None:
    p = dl.get_provider("lm_studio")
    assert p.id == "lm_studio"
    assert p.base_url == "http://localhost:1234/v1"
    assert p.models_endpoint == "/models"
    assert p.requires_api_key is False
    assert p.allow_base_url_edit is False


def test_get_unknown_provider_raises() -> None:
    with pytest.raises(dl.DictateLLMError, match="unknown provider"):
        dl.get_provider("nope")


def test_custom_provider_allows_base_url_edit() -> None:
    assert dl.get_provider("custom").allow_base_url_edit is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_dictate_llm.py -v`
Expected: ImportError — module does not exist.

- [ ] **Step 3: Implement the catalog**

Create `agent_doctor/dictate_llm.py`:

```python
"""OpenAI-compatible LLM provider catalog + probe helpers for dictate.

Three providers ship in the catalog:

- ``lm_studio``  → http://localhost:1234/v1  (LM Studio's OpenAI-compatible server)
- ``ollama``     → http://localhost:11434/v1 (Ollama's OpenAI-compatible server)
- ``custom``     → user-supplied URL, optional API key, free-form

Probing a provider issues ``GET <base_url>/models`` with a short timeout and
returns the parsed model list. We never store API keys in this module — they
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
        f"unknown provider {provider_id!r}; expected one of: {', '.join(p.id for p in _PROVIDERS)}"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_dictate_llm.py -v`
Expected: 4 passing.

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/dictate_llm.py tests/test_dictate_llm.py
git commit -m "feat(dictate): provider catalog (LM Studio / Ollama / Custom)"
```

---

## Task 2: Add `probe()` for `/v1/models` discovery

**Files:**
- Modify: `agent_doctor/dictate_llm.py`
- Test: `tests/test_dictate_llm.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dictate_llm.py`:

```python
@pytest.fixture
def fake_openai_server() -> Iterator[tuple[str, dict[str, object]]]:
    payloads: dict[str, object] = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/v1/models":
                body = payloads.get("body", {"data": []})
                status = int(payloads.get("status", 200))
                if isinstance(body, (bytes, str)):
                    raw = body.encode("utf-8") if isinstance(body, str) else body
                else:
                    raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            return

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = http.server.HTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/v1", payloads
    finally:
        server.shutdown()
        thread.join(timeout=2.0)


def test_probe_reachable_returns_model_ids(
    fake_openai_server: tuple[str, dict[str, object]],
) -> None:
    base_url, payloads = fake_openai_server
    payloads["body"] = {
        "object": "list",
        "data": [
            {"id": "qwen2.5-7b-instruct", "object": "model"},
            {"id": "llama-3.1-8b", "object": "model"},
        ],
    }

    result = dl.probe(base_url, "/models")
    assert result.reachable is True
    assert result.models == ["qwen2.5-7b-instruct", "llama-3.1-8b"]
    assert result.error is None


def test_probe_404_returns_unreachable_with_reason(
    fake_openai_server: tuple[str, dict[str, object]],
) -> None:
    base_url, payloads = fake_openai_server
    payloads["status"] = 404
    payloads["body"] = "nope"
    result = dl.probe(base_url, "/models")
    assert result.reachable is False
    assert "404" in (result.error or "")


def test_probe_unreachable_host_does_not_raise() -> None:
    result = dl.probe("http://127.0.0.1:1", "/models", timeout=1.0)
    assert result.reachable is False
    assert result.error is not None
    assert result.models == []


def test_probe_all_returns_one_row_per_provider(
    fake_openai_server: tuple[str, dict[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    base_url, payloads = fake_openai_server
    payloads["body"] = {"data": [{"id": "stub"}]}

    # Override every catalog entry to point at the fake server so the test
    # never hits localhost ports for real.
    fake_providers = tuple(
        dl.Provider(
            id=p.id,
            label=p.label,
            base_url=base_url,
            models_endpoint=p.models_endpoint,
            requires_api_key=p.requires_api_key,
            allow_base_url_edit=p.allow_base_url_edit,
        )
        for p in dl.providers()
    )
    monkeypatch.setattr(dl, "_PROVIDERS", fake_providers)
    rows = dl.probe_all(timeout=1.0)
    assert {r.provider_id for r in rows} == {"lm_studio", "ollama", "custom"}
    for row in rows:
        assert row.reachable is True
        assert row.models == ["stub"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_dictate_llm.py -v`
Expected: 4 new failures — `probe`, `probe_all`, `ProbeResult` not defined.

- [ ] **Step 3: Implement probe + probe_all**

Append to `agent_doctor/dictate_llm.py`:

```python
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
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_dictate_llm.py -v`
Expected: 8 passing.

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/dictate_llm.py tests/test_dictate_llm.py
git commit -m "feat(dictate): /v1/models probe with timeout-safe error reporting"
```

---

## Task 3: Add settings-aware `llm_config()` to `dictate_llm.py`

**Files:**
- Modify: `agent_doctor/dictate_llm.py`, `agent_doctor/dictate.py`
- Test: `tests/test_dictate_llm.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dictate_llm.py`:

```python
def test_llm_config_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI > env > settings > provider default."""

    from agent_doctor import dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    settings = ds.replace_section(
        ds.default_settings(),
        llm=ds.LLMSettings(
            provider_id="custom",
            base_url="http://from-settings:9000/v1",
            model="settings-model",
            timeout_s=42,
        ),
    )
    ds.save(settings)

    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_LLM_URL", "http://from-env:9001/v1/chat/completions")
    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_LLM_MODEL", "env-model")
    monkeypatch.delenv("AGENT_DOCTOR_DICTATE_LLM_KEY", raising=False)

    # Env beats settings.
    cfg = dl.llm_config()
    assert cfg.url == "http://from-env:9001/v1/chat/completions"
    assert cfg.model == "env-model"
    assert cfg.timeout == 42  # timeout still comes from settings

    # CLI beats env.
    cfg = dl.llm_config(url="http://cli/v1/chat/completions", model="cli-model")
    assert cfg.url == "http://cli/v1/chat/completions"
    assert cfg.model == "cli-model"


def test_llm_config_falls_back_to_provider_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_doctor import dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    monkeypatch.delenv("AGENT_DOCTOR_DICTATE_LLM_URL", raising=False)
    monkeypatch.delenv("AGENT_DOCTOR_DICTATE_LLM_MODEL", raising=False)
    monkeypatch.delenv("AGENT_DOCTOR_DICTATE_LLM_KEY", raising=False)
    # Default settings -> lm_studio provider -> http://localhost:1234/v1
    cfg = dl.llm_config()
    assert cfg.url.startswith("http://localhost:1234/v1")
    assert cfg.url.endswith("/chat/completions")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_dictate_llm.py::test_llm_config_precedence -v`
Expected: FAIL — `llm_config` not defined in `dictate_llm`.

- [ ] **Step 3: Implement `llm_config()`**

Append to `agent_doctor/dictate_llm.py`:

```python
import os
from agent_doctor.dictate import LLMConfig, ENV_LLM_URL, ENV_LLM_MODEL, ENV_LLM_KEY


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

    The settings ``base_url`` is OpenAI-style root (``.../v1``) — we append
    ``/chat/completions`` so the existing ``_default_llm_call`` continues to
    work. Existing env var keeps the legacy literal value so users with
    pre-existing config are unaffected.
    """

    from . import dictate_settings as _ds  # local import to avoid cycle

    settings = _ds.load()
    provider = get_provider(settings.llm.provider_id)
    settings_url = (settings.llm.base_url or provider.base_url).rstrip("/") + "/chat/completions"
    settings_model = settings.llm.model
    settings_key = None  # api_key_ref handling deferred to Phase 6; settings does not store secrets

    resolved_url = url or os.environ.get(ENV_LLM_URL) or settings_url
    resolved_model = model or os.environ.get(ENV_LLM_MODEL) or settings_model or "default"
    resolved_key = api_key or os.environ.get(ENV_LLM_KEY) or settings_key

    return LLMConfig(
        url=resolved_url,
        model=resolved_model,
        api_key=resolved_key,
        timeout=float(settings.llm.timeout_s),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_dictate_llm.py -v`
Expected: 10 passing.

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/dictate_llm.py tests/test_dictate_llm.py
git commit -m "feat(dictate): settings-aware llm_config() with CLI > env > settings precedence"
```

---

## Task 4: Replace four mode prompts with a single `OPTIMIZE_PROMPT`

**Files:**
- Modify: `agent_doctor/dictate.py`
- Test: `tests/test_dictate.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dictate.py`:

```python
def test_optimize_prompt_is_used_for_every_non_raw_mode() -> None:
    """Phase 2: all non-raw modes collapse to OPTIMIZE_PROMPT."""

    text_chat = dictate.mode_system_prompt("chat")
    text_coding = dictate.mode_system_prompt("coding")
    text_research = dictate.mode_system_prompt("research")
    text_optimize = dictate.mode_system_prompt("optimize")
    assert text_chat == text_coding == text_research == text_optimize
    assert "optimized for any downstream LLM" in text_chat


def test_optimize_prompt_honours_settings_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_doctor import dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    custom = "Custom override prompt content"
    settings = ds.replace_section(
        ds.default_settings(),
        llm=ds.LLMSettings(optimize_prompt=custom),
    )
    ds.save(settings)
    assert dictate.mode_system_prompt("optimize") == custom


def test_raw_mode_still_rejects_prompt_request() -> None:
    with pytest.raises(dictate.DictateError, match="raw"):
        dictate.mode_system_prompt("raw")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_dictate.py -v -k optimize_prompt`
Expected: failures — `optimize` is not a recognised mode and `_MODE_PROMPTS` still differs.

- [ ] **Step 3: Replace the mode prompts**

In `agent_doctor/dictate.py`:

1. Replace the constants block. Find:

```python
DEFAULT_MODE = "chat"
SUPPORTED_MODES = ("chat", "coding", "research", "raw")
```

Change to:

```python
DEFAULT_MODE = "optimize"
SUPPORTED_MODES = ("optimize", "chat", "coding", "research", "raw")
DEPRECATED_MODES = ("chat", "coding", "research")
```

2. Replace the `_MODE_PROMPTS` and `mode_system_prompt` block (currently lines ~116–175). Replace with:

```python
_BASE_RULES = (
    "You are a prompt rewriter. Your job is to turn a transcribed spoken note "
    "into a written prompt the user can paste into another AI application.\n\n"
    "Strict rules:\n"
    "- Preserve the user's intent and every concrete detail they spoke.\n"
    "- Do not invent facts, entities, file paths, numbers, names, or scope.\n"
    "- Do not add a role-play preamble (no 'You are an expert ...').\n"
    "- Do not add safety disclaimers, hedges, or generic encouragement.\n"
    "- Drop filler words ('um', 'uh', 'like', '就是', '那个', '对吧') and "
    "  obvious mis-starts, but keep all substantive content.\n"
    "- Fix obvious transcription typos when the intended word is unambiguous.\n"
    "- Output the rewritten prompt ONLY. No quoting, no commentary, no "
    "  'Here is the rewritten prompt:' header.\n"
)

OPTIMIZE_PROMPT = (
    _BASE_RULES
    + "\nStyle: a clean, written prompt optimized for any downstream LLM. "
    "Use the user's language. No padding, no role-play preamble, no header — "
    "output the rewritten prompt only."
)


def mode_system_prompt(mode: str) -> str:
    """Return the system prompt for ``mode``.

    Phase 2 collapses every non-raw mode into a single ``OPTIMIZE_PROMPT``.
    Settings may override the literal text via ``llm.optimize_prompt``.

    ``raw`` has no enhancement and therefore no system prompt; callers should
    check :func:`is_raw_mode` before invoking the enhancer.
    """

    if mode == "raw":
        raise DictateError("mode 'raw' bypasses the LLM enhancer")
    if mode not in SUPPORTED_MODES:
        raise DictateError(
            f"unknown dictate mode '{mode}'; expected one of "
            f"{', '.join(SUPPORTED_MODES)}"
        )
    try:
        from . import dictate_settings as _ds
        override = _ds.load().llm.optimize_prompt
    except Exception:  # noqa: BLE001 - settings must never break prompt lookup
        override = None
    return override or OPTIMIZE_PROMPT
```

- [ ] **Step 4: Update existing parametrised test that excludes "raw"**

In `tests/test_dictate.py` find:

```python
@pytest.mark.parametrize("mode", [m for m in SUPPORTED_MODES if m != "raw"])
def test_mode_system_prompt_includes_anti_fabrication_rule(mode: str) -> None:
    text = mode_system_prompt(mode)
    assert "Do not invent" in text
    assert "Output the rewritten prompt ONLY" in text
```

Leave the assertion unchanged — the new `OPTIMIZE_PROMPT` retains both strings. The parametrisation now covers `optimize`, `chat`, `coding`, `research` and all should pass.

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/test_dictate.py -v -k "mode_system_prompt or optimize_prompt or is_raw"`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add agent_doctor/dictate.py tests/test_dictate.py
git commit -m "feat(dictate): single OPTIMIZE_PROMPT for every non-raw mode"
```

---

## Task 5: Emit deprecation warning + alias old modes

**Files:**
- Modify: `agent_doctor/cli.py`
- Test: `tests/test_dictate.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dictate.py`:

```python
def test_cli_dictate_warns_on_deprecated_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--mode chat` etc. work but print a stderr deprecation notice."""

    from agent_doctor import cli, dictate as _d

    # No real recording will run; we just exercise argument parsing + the
    # deprecation hook. _cmd_dictate_start aborts cleanly with an audio error
    # when no recorder is on PATH. We monkeypatch _detect_recorder to raise so
    # the path runs deterministically.
    def _no_recorder(*_a: object, **_k: object) -> str:
        raise _d.DictateError("no recorder")

    monkeypatch.setattr(_d, "_detect_recorder", _no_recorder)
    monkeypatch.setattr(_d, "default_state_dir", lambda: tmp_path)

    rc = cli.main(["dictate", "start", "--mode", "chat"])
    captured = capsys.readouterr()
    assert "deprecated" in captured.err.lower()
    assert "optimize" in captured.err.lower()
    # The actual start fails because we stubbed out the recorder; rc == 2 is fine.
    assert rc == 2
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_dictate.py::test_cli_dictate_warns_on_deprecated_mode -v`
Expected: FAIL — no deprecation warning emitted.

- [ ] **Step 3: Add the deprecation hook to the dictate CLI dispatcher**

In `agent_doctor/cli.py`, find `_cmd_dictate_start` (around line 1626). Replace its body's first line:

```python
def _cmd_dictate_start(args: argparse.Namespace) -> int:
    from . import dictate as _d

    mode = getattr(args, "mode", None) or _d.DEFAULT_MODE
```

with:

```python
def _cmd_dictate_start(args: argparse.Namespace) -> int:
    from . import dictate as _d

    raw_mode = getattr(args, "mode", None)
    mode = _maybe_deprecate_mode(raw_mode) or _d.DEFAULT_MODE
```

Add the helper near the top of the dictate handler section (after `_DICTATE_DEFAULT_MODE` is imported, so the symbol exists), e.g. just above `_cmd_dictate_start`:

```python
def _maybe_deprecate_mode(mode: Optional[str]) -> Optional[str]:
    """If ``mode`` is one of the deprecated chat/coding/research aliases, emit a
    one-line stderr warning and collapse to 'optimize'. Returns the canonical mode."""

    from . import dictate as _d

    if mode in _d.DEPRECATED_MODES:
        print(
            f"agent-doctor: --mode {mode!r} is deprecated; using 'optimize' "
            "(see CHANGELOG)",
            file=sys.stderr,
        )
        return "optimize"
    return mode
```

Update `_dictate_finish` (around line 1789) similarly. Find:

```python
    cli_mode = getattr(args, "mode", None)
    mode = cli_mode if cli_mode is not None else state.mode
```

Change to:

```python
    cli_mode = _maybe_deprecate_mode(getattr(args, "mode", None))
    mode = cli_mode if cli_mode is not None else _maybe_deprecate_mode(state.mode) or state.mode
```

- [ ] **Step 4: Run the test**

Run: `python3 -m pytest tests/test_dictate.py::test_cli_dictate_warns_on_deprecated_mode -v`
Expected: green.

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/cli.py tests/test_dictate.py
git commit -m "feat(dictate): deprecate chat/coding/research modes; collapse to 'optimize'"
```

---

## Task 6: Register `dictate llm` subcommands

**Files:**
- Modify: `agent_doctor/cli.py`
- Test: `tests/test_cli_subcommand_registration.py`

- [ ] **Step 1: Add registration**

In `agent_doctor/cli.py`, after the `dictate models` block from Phase 1 Task 6, append:

```python
    dictate_llm = dictate_subs.add_parser(
        "llm",
        help="Configure the LLM enhancer (LM Studio / Ollama / Custom).",
    )
    dictate_llm_subs = dictate_llm.add_subparsers(
        dest="dictate_llm_cmd", required=True
    )

    llm_probe = dictate_llm_subs.add_parser(
        "probe", help="Probe every known provider's /v1/models endpoint."
    )
    llm_probe.add_argument("--json", action="store_true")
    llm_probe.set_defaults(func=_cmd_dictate_llm_probe)

    llm_set = dictate_llm_subs.add_parser("set", help="Update the active LLM provider.")
    llm_set.add_argument("--provider", choices=["lm_studio", "ollama", "custom"], required=True)
    llm_set.add_argument("--model", default=None, help="OpenAI-style model id.")
    llm_set.add_argument("--url", default=None, help="Override base URL (Custom provider only).")
    llm_set.set_defaults(func=_cmd_dictate_llm_set)

    llm_current = dictate_llm_subs.add_parser("current", help="Show active provider + model.")
    llm_current.add_argument("--json", action="store_true")
    llm_current.set_defaults(func=_cmd_dictate_llm_current)

    llm_test = dictate_llm_subs.add_parser(
        "test", help="Round-trip a canned transcript through the configured provider."
    )
    llm_test.add_argument(
        "text", nargs="?", default="please rewrite this as a clean prompt", help="Text to enhance."
    )
    llm_test.set_defaults(func=_cmd_dictate_llm_test)
```

Add handler functions at end of `cli.py`:

```python
def _cmd_dictate_llm_probe(args: argparse.Namespace) -> int:
    from . import dictate_llm as _dl

    rows = _dl.probe_all()
    payload = [
        {
            "provider_id": r.provider_id,
            "base_url": r.base_url,
            "reachable": r.reachable,
            "models": r.models,
            "error": r.error,
        }
        for r in rows
    ]
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    for row in payload:
        status = "✓" if row["reachable"] else "✗"
        head = f"{status} {row['provider_id']:<10} {row['base_url']}"
        if row["reachable"]:
            print(f"{head}  ({len(row['models'])} model(s))")
            for mid in row["models"]:
                print(f"    - {mid}")
        else:
            print(f"{head}  unreachable: {row['error']}")
    return 0


def _cmd_dictate_llm_set(args: argparse.Namespace) -> int:
    from . import dictate_settings as _ds
    from . import dictate_llm as _dl

    try:
        provider = _dl.get_provider(args.provider)
    except _dl.DictateLLMError as exc:
        print(f"agent-doctor: {exc}", file=sys.stderr)
        return 2
    if args.url and not provider.allow_base_url_edit:
        print(
            f"agent-doctor: provider {provider.id!r} does not allow base-url override; "
            "use --provider custom to supply a URL",
            file=sys.stderr,
        )
        return 2
    settings = _ds.load()
    new_llm = _ds.LLMSettings(
        provider_id=provider.id,
        base_url=args.url or provider.base_url,
        model=args.model,
        api_key_ref=settings.llm.api_key_ref,
        timeout_s=settings.llm.timeout_s,
        optimize_prompt=settings.llm.optimize_prompt,
    )
    _ds.save(_ds.replace_section(settings, llm=new_llm))
    print(f"provider: {provider.id}\nurl: {new_llm.base_url}\nmodel: {new_llm.model or '(none)'}")
    return 0


def _cmd_dictate_llm_current(args: argparse.Namespace) -> int:
    from . import dictate_settings as _ds

    settings = _ds.load()
    payload = {
        "provider_id": settings.llm.provider_id,
        "base_url": settings.llm.base_url,
        "model": settings.llm.model,
        "timeout_s": settings.llm.timeout_s,
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(
        f"provider: {payload['provider_id']}\n"
        f"url: {payload['base_url']}\n"
        f"model: {payload['model'] or '(none)'}"
    )
    return 0


def _cmd_dictate_llm_test(args: argparse.Namespace) -> int:
    from . import dictate as _d
    from . import dictate_llm as _dl

    try:
        cfg = _dl.llm_config()
        result = _d.enhance_prompt(args.text, mode="optimize", config=cfg)
    except _d.DictateError as exc:
        print(f"agent-doctor: {exc}", file=sys.stderr)
        return 2
    print(result)
    return 0
```

- [ ] **Step 2: Update the registration smoke test**

Append to `tests/test_cli_subcommand_registration.py`:

```python
EXPECTED_DICTATE_LLM_SUBCOMMANDS = {"probe", "set", "current", "test"}


def test_dictate_llm_subcommands_registered() -> None:
    parser = cli.build_parser()
    dictate_sub = _nested_subparser(parser, "dictate")
    llm_sub = _nested_subparser(dictate_sub, "llm")
    assert _subparser_choices(llm_sub) >= EXPECTED_DICTATE_LLM_SUBCOMMANDS
```

- [ ] **Step 3: Run the tests**

Run: `python3 -m pytest tests/test_cli_subcommand_registration.py tests/test_dictate_llm.py -v`
Expected: all green.

- [ ] **Step 4: Hand-run a probe**

```bash
python3 -m agent_doctor.cli dictate llm probe
```

Expected: three rows (`lm_studio`, `ollama`, `custom`) with either reachable models or unreachable errors. No crashes.

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/cli.py tests/test_cli_subcommand_registration.py
git commit -m "feat(cli): register dictate llm {probe,set,current,test}"
```

---

## Task 7: Switch `_dictate_finish` to use the new `llm_config()`

**Files:**
- Modify: `agent_doctor/cli.py`
- Test: `tests/test_dictate.py`

- [ ] **Step 1: Replace the existing `llm_config_from_env` call site**

In `agent_doctor/cli.py` `_dictate_finish`, find:

```python
    llm_config = _d.llm_config_from_env(
        url=getattr(args, "llm_url", None),
        model=getattr(args, "llm_model", None),
        api_key=getattr(args, "llm_key", None),
    )
```

Replace with:

```python
    from . import dictate_llm as _dl
    llm_config = _dl.llm_config(
        url=getattr(args, "llm_url", None),
        model=getattr(args, "llm_model", None),
        api_key=getattr(args, "llm_key", None),
    )
```

- [ ] **Step 2: Keep `llm_config_from_env` as a thin alias**

In `agent_doctor/dictate.py`, replace the body of `llm_config_from_env`:

```python
def llm_config_from_env(
    *,
    url: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
) -> LLMConfig:
    """Backwards-compatible shim around :func:`dictate_llm.llm_config`."""

    from . import dictate_llm as _dl
    return _dl.llm_config(url=url, model=model, api_key=api_key)
```

- [ ] **Step 3: Run the full dictate suite**

Run: `python3 -m pytest tests/test_dictate.py tests/test_dictate_llm.py -v`
Expected: all green; the existing `test_llm_config_from_env_defaults` and `test_llm_config_from_env_overrides` still pass via the shim.

- [ ] **Step 4: Commit**

```bash
git add agent_doctor/dictate.py agent_doctor/cli.py
git commit -m "refactor(dictate): _dictate_finish uses dictate_llm.llm_config"
```

---

## Task 8: Update README + run the whole suite green

- [ ] **Step 1: Document the new commands**

In `README.md`, after the model picker section from Phase 1, add:

```markdown
### LLM enhancer

```bash
# Which providers can we see?
agent-doctor dictate llm probe

# Point at LM Studio with a specific model
agent-doctor dictate llm set --provider lm_studio --model qwen2.5-7b-instruct

# Or Ollama
agent-doctor dictate llm set --provider ollama --model llama3.1:8b

# Custom OpenAI-compatible endpoint
agent-doctor dictate llm set --provider custom --url http://localhost:8000/v1 --model whatever

# Show the active config
agent-doctor dictate llm current

# Round-trip a canned transcript
agent-doctor dictate llm test "rewrite this as a clean prompt"
```

Phase 2 collapses the old `--mode chat|coding|research` flags into a single "optimize for any downstream LLM" prompt. The flags continue to parse but emit a deprecation warning and behave as `--mode optimize`.
```

- [ ] **Step 2: Run the whole suite**

Run: `python3 -m pytest -q`
Expected: all green.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(dictate): document llm subcommand and mode deprecation"
git tag dictate-phase-2-complete
```

---

## Phase 2 verification checklist

- [ ] `python3 -m pytest -q` is green.
- [ ] `agent-doctor dictate llm probe` returns three rows; reachable when LM Studio is open.
- [ ] `agent-doctor dictate llm set --provider lm_studio --model <id>` updates `~/.agent-doctor/dictate.json`.
- [ ] `agent-doctor dictate llm test "rewrite this"` round-trips through the active provider end-to-end.
- [ ] `agent-doctor dictate toggle` followed by a real recording now uses the configured provider — verify via `--timing` and your LM Studio request log.
- [ ] `agent-doctor dictate start --mode chat` prints a deprecation notice and behaves as `optimize`.
- [ ] No new runtime dependencies introduced.
