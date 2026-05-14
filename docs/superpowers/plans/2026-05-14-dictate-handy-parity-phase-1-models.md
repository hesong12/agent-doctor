# Dictate Phase 1 — Model picker + downloads Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `agent-doctor dictate models {list,current,download,set,remove,doctor}` plus a versioned `~/.agent-doctor/dictate.json` settings file, so users can pick + download whisper GGML models from an allow-listed catalog and the dictate pipeline uses the selected model by default.

**Architecture:** Two new modules. `dictate_settings.py` owns the JSON settings file (load/save/migrate, frozen dataclasses). `dictate_models.py` owns the static catalog of authorized Hugging Face GGML URLs and the download helper (urllib streaming, SHA-256 verify, atomic install). A new `dictate models` subcommand group in `cli.py` wires them together. `dictate.transcribe()` is updated to consult settings when no `model_name` is supplied.

**Tech Stack:** Python 3.11 stdlib only (json, urllib.request, hashlib, http.server for tests, pytest, argparse). No new runtime dependencies.

**Spec:** `docs/superpowers/specs/2026-05-14-dictate-handy-parity-design.md` §4 and §5.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `agent_doctor/dictate_settings.py` | Create — load/save/validate the `~/.agent-doctor/dictate.json` file. Versioned, atomic writes, dataclass interface. |
| `agent_doctor/dictate_models.py` | Create — static catalog of authorized whisper.cpp models + streaming download + SHA-256 verify. |
| `agent_doctor/dictate.py` | Modify — `transcribe()` falls back to settings when `model_name=None`. |
| `agent_doctor/cli.py` | Modify — register `dictate models` subcommand group and its 6 handlers. |
| `tests/test_dictate_settings.py` | Create — round-trip, validation, migration, atomic write. |
| `tests/test_dictate_models.py` | Create — catalog, allow-list, download success/failure/sha-mismatch/partial, set/remove. |
| `tests/test_dictate.py` | Modify — add tests covering the settings fallback in `transcribe()`. |
| `tests/test_cli_subcommand_registration.py` | Modify — add the new subcommands to the registered-set assertion. |

---

## Task 1: Create `dictate_settings.py` skeleton + dataclasses

**Files:**
- Create: `agent_doctor/dictate_settings.py`
- Test: `tests/test_dictate_settings.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_dictate_settings.py`:

```python
"""Tests for ~/.agent-doctor/dictate.json settings storage."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from agent_doctor import dictate_settings as ds


def test_defaults_have_expected_shape() -> None:
    defaults = ds.default_settings()
    assert defaults.version == 1
    assert defaults.transcription.model_id is None
    assert defaults.transcription.model_path is None
    assert defaults.transcription.language == "auto"
    assert defaults.transcription.extra_buffer_ms == 150
    assert defaults.llm.provider_id == "lm_studio"
    assert defaults.llm.base_url == "http://localhost:1234/v1"
    assert defaults.llm.model is None
    assert defaults.llm.api_key_ref is None
    assert defaults.llm.timeout_s == 30
    assert defaults.llm.optimize_prompt is None
    assert defaults.hotkey.binding == "ctrl+option+space"
    assert defaults.hotkey.push_to_talk is True
    assert defaults.hotkey.daemon_enabled is False
    assert defaults.paste.auto_paste is False
    assert defaults.paste.paste_delay_ms == 60
    assert defaults.paste.last_permission_check is None
    assert defaults.pet.animate_listening is True
    assert defaults.pet.animate_thinking is True


def test_dataclasses_are_frozen() -> None:
    defaults = ds.default_settings()
    with pytest.raises(Exception):
        defaults.transcription.language = "en"  # type: ignore[misc]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_dictate_settings.py -v`

Expected: ImportError or attribute errors — `dictate_settings` module does not exist.

- [ ] **Step 3: Write the module**

Create `agent_doctor/dictate_settings.py`:

```python
"""Local-first settings storage for the dictate pipeline.

Stores user preferences for:
- Whisper model selection
- LLM provider + URL + model
- Global hotkey binding
- Auto-paste configuration
- Pet animation toggles

File location: ``~/.agent-doctor/dictate.json``. Schema-versioned, atomic
writes, mode 0600. Secrets (LLM API keys) live in the system keychain via
``agent_doctor.settings``; this file stores only references.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

CONFIG_DIR = Path("~/.agent-doctor").expanduser()
CONFIG_FILE = CONFIG_DIR / "dictate.json"
SCHEMA_VERSION = 1
_FILE_MODE = 0o600
_DIR_MODE = 0o700


class DictateSettingsError(RuntimeError):
    """Raised on invalid / unparseable settings files."""


@dataclass(frozen=True)
class TranscriptionSettings:
    model_id: Optional[str] = None
    model_path: Optional[str] = None
    language: str = "auto"
    extra_buffer_ms: int = 150


@dataclass(frozen=True)
class LLMSettings:
    provider_id: str = "lm_studio"
    base_url: str = "http://localhost:1234/v1"
    model: Optional[str] = None
    api_key_ref: Optional[str] = None
    timeout_s: int = 30
    optimize_prompt: Optional[str] = None


@dataclass(frozen=True)
class HotkeySettings:
    binding: str = "ctrl+option+space"
    push_to_talk: bool = True
    daemon_enabled: bool = False


@dataclass(frozen=True)
class PasteSettings:
    auto_paste: bool = False
    paste_delay_ms: int = 60
    last_permission_check: Optional[str] = None


@dataclass(frozen=True)
class PetSettings:
    animate_listening: bool = True
    animate_thinking: bool = True


@dataclass(frozen=True)
class DictateSettings:
    version: int = SCHEMA_VERSION
    transcription: TranscriptionSettings = field(default_factory=TranscriptionSettings)
    llm: LLMSettings = field(default_factory=LLMSettings)
    hotkey: HotkeySettings = field(default_factory=HotkeySettings)
    paste: PasteSettings = field(default_factory=PasteSettings)
    pet: PetSettings = field(default_factory=PetSettings)


def default_settings() -> DictateSettings:
    """Return a fresh DictateSettings populated with defaults."""

    return DictateSettings()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_dictate_settings.py -v`

Expected: 2 passing.

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/dictate_settings.py tests/test_dictate_settings.py
git commit -m "feat(dictate): scaffold versioned dictate.json settings dataclasses"
```

---

## Task 2: Add load/save with atomic write and 0600 mode

**Files:**
- Modify: `agent_doctor/dictate_settings.py`
- Test: `tests/test_dictate_settings.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dictate_settings.py`:

```python
def test_save_and_load_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "dictate.json"
    monkeypatch.setattr(ds, "CONFIG_FILE", cfg)
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)

    settings = ds.default_settings()
    settings = ds.replace_section(
        settings,
        transcription=ds.TranscriptionSettings(
            model_id="ggml-small",
            model_path=str(tmp_path / "small.bin"),
            language="en",
            extra_buffer_ms=200,
        ),
    )
    ds.save(settings)

    loaded = ds.load()
    assert loaded.transcription.model_id == "ggml-small"
    assert loaded.transcription.model_path == str(tmp_path / "small.bin")
    assert loaded.transcription.language == "en"
    assert loaded.transcription.extra_buffer_ms == 200


def test_load_missing_returns_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "missing.json")
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    loaded = ds.load()
    assert loaded == ds.default_settings()


def test_save_writes_0600(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "dictate.json"
    monkeypatch.setattr(ds, "CONFIG_FILE", cfg)
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    ds.save(ds.default_settings())
    mode = stat.S_IMODE(cfg.stat().st_mode)
    assert mode == 0o600


def test_load_corrupt_json_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "dictate.json"
    cfg.write_text("{ not json")
    monkeypatch.setattr(ds, "CONFIG_FILE", cfg)
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    with pytest.raises(ds.DictateSettingsError, match="parse"):
        ds.load()


def test_load_future_version_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "dictate.json"
    cfg.write_text(json.dumps({"version": 999}))
    monkeypatch.setattr(ds, "CONFIG_FILE", cfg)
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    with pytest.raises(ds.DictateSettingsError, match="version"):
        ds.load()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_dictate_settings.py -v`
Expected: 4 failing (load/save/replace_section not defined).

- [ ] **Step 3: Implement load/save/replace_section**

Append to `agent_doctor/dictate_settings.py`:

```python
def _to_dict(settings: DictateSettings) -> dict[str, Any]:
    return {
        "version": settings.version,
        "transcription": {
            "model_id": settings.transcription.model_id,
            "model_path": settings.transcription.model_path,
            "language": settings.transcription.language,
            "extra_buffer_ms": settings.transcription.extra_buffer_ms,
        },
        "llm": {
            "provider_id": settings.llm.provider_id,
            "base_url": settings.llm.base_url,
            "model": settings.llm.model,
            "api_key_ref": settings.llm.api_key_ref,
            "timeout_s": settings.llm.timeout_s,
            "optimize_prompt": settings.llm.optimize_prompt,
        },
        "hotkey": {
            "binding": settings.hotkey.binding,
            "push_to_talk": settings.hotkey.push_to_talk,
            "daemon_enabled": settings.hotkey.daemon_enabled,
        },
        "paste": {
            "auto_paste": settings.paste.auto_paste,
            "paste_delay_ms": settings.paste.paste_delay_ms,
            "last_permission_check": settings.paste.last_permission_check,
        },
        "pet": {
            "animate_listening": settings.pet.animate_listening,
            "animate_thinking": settings.pet.animate_thinking,
        },
    }


def _from_dict(payload: dict[str, Any]) -> DictateSettings:
    if not isinstance(payload, dict):
        raise DictateSettingsError("expected JSON object at top level")
    version = payload.get("version", SCHEMA_VERSION)
    if not isinstance(version, int):
        raise DictateSettingsError("'version' must be an integer")
    if version > SCHEMA_VERSION:
        raise DictateSettingsError(
            f"unsupported settings version {version} (this build supports {SCHEMA_VERSION})"
        )
    t = payload.get("transcription") or {}
    l = payload.get("llm") or {}
    h = payload.get("hotkey") or {}
    p = payload.get("paste") or {}
    pet = payload.get("pet") or {}
    return DictateSettings(
        version=version,
        transcription=TranscriptionSettings(
            model_id=t.get("model_id"),
            model_path=t.get("model_path"),
            language=t.get("language", "auto"),
            extra_buffer_ms=int(t.get("extra_buffer_ms", 150)),
        ),
        llm=LLMSettings(
            provider_id=l.get("provider_id", "lm_studio"),
            base_url=l.get("base_url", "http://localhost:1234/v1"),
            model=l.get("model"),
            api_key_ref=l.get("api_key_ref"),
            timeout_s=int(l.get("timeout_s", 30)),
            optimize_prompt=l.get("optimize_prompt"),
        ),
        hotkey=HotkeySettings(
            binding=h.get("binding", "ctrl+option+space"),
            push_to_talk=bool(h.get("push_to_talk", True)),
            daemon_enabled=bool(h.get("daemon_enabled", False)),
        ),
        paste=PasteSettings(
            auto_paste=bool(p.get("auto_paste", False)),
            paste_delay_ms=int(p.get("paste_delay_ms", 60)),
            last_permission_check=p.get("last_permission_check"),
        ),
        pet=PetSettings(
            animate_listening=bool(pet.get("animate_listening", True)),
            animate_thinking=bool(pet.get("animate_thinking", True)),
        ),
    )


def replace_section(settings: DictateSettings, **overrides: Any) -> DictateSettings:
    """Return a new DictateSettings with the given top-level sections replaced.

    Example: ``replace_section(s, llm=LLMSettings(...))``.
    """

    return replace(settings, **overrides)


def _ensure_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, _DIR_MODE)
    except OSError:
        # Best-effort: some filesystems (e.g. mounted tmpfs in tests) reject chmod.
        pass


def _atomic_write(dest: Path, body: bytes) -> None:
    """Write ``body`` to ``dest`` atomically with mode 0600."""

    _ensure_dir()
    fd, tmp_name = tempfile.mkstemp(prefix=".dictate.json.", dir=str(dest.parent))
    try:
        os.write(fd, body)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp_name, _FILE_MODE)
    os.replace(tmp_name, dest)


def save(settings: DictateSettings) -> Path:
    """Persist ``settings`` to ``CONFIG_FILE`` atomically. Returns the path."""

    body = json.dumps(_to_dict(settings), indent=2, sort_keys=True).encode("utf-8")
    _atomic_write(CONFIG_FILE, body)
    return CONFIG_FILE


def load() -> DictateSettings:
    """Load settings from ``CONFIG_FILE`` or return defaults if missing."""

    if not CONFIG_FILE.exists():
        return default_settings()
    try:
        payload = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DictateSettingsError(
            f"failed to parse {CONFIG_FILE}: {exc.msg} at line {exc.lineno}"
        ) from exc
    return _from_dict(payload)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_dictate_settings.py -v`
Expected: 6 passing.

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/dictate_settings.py tests/test_dictate_settings.py
git commit -m "feat(dictate): persist dictate.json with atomic 0600 write + load"
```

---

## Task 3: Create `dictate_models.py` with the authorized catalog

**Files:**
- Create: `agent_doctor/dictate_models.py`
- Test: `tests/test_dictate_models.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_dictate_models.py`:

```python
"""Tests for the whisper.cpp model catalog + downloader."""

from __future__ import annotations

import hashlib
import http.server
import json
import socket
import threading
from pathlib import Path
from typing import Iterator

import pytest

from agent_doctor import dictate_models as dm


def test_catalog_is_non_empty_and_well_formed() -> None:
    catalog = dm.catalog()
    assert len(catalog) >= 4
    ids = {entry.id for entry in catalog}
    # Sanity: the Handy-compatible default must be present.
    assert "ggml-large-v3-turbo" in ids
    for entry in catalog:
        assert entry.url.startswith(
            "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
        )
        assert entry.size_bytes > 0
        assert len(entry.sha256) == 64
        assert entry.display_name
        assert isinstance(entry.recommended_for, tuple)


def test_get_returns_catalog_entry() -> None:
    entry = dm.get("ggml-large-v3-turbo")
    assert entry.id == "ggml-large-v3-turbo"


def test_get_unknown_raises() -> None:
    with pytest.raises(dm.DictateModelsError, match="unknown"):
        dm.get("does-not-exist")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_dictate_models.py -v`
Expected: ImportError — module not found.

- [ ] **Step 3: Implement the catalog**

Create `agent_doctor/dictate_models.py`:

```python
"""Authorized whisper.cpp GGML model catalog + downloader.

All models come from ``https://huggingface.co/ggerganov/whisper.cpp`` so we
allow-list that single origin. Catalog SHA-256s were captured at design time
on 2026-05-14; ``models doctor`` re-verifies them.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

ALLOW_LIST = ("https://huggingface.co/ggerganov/whisper.cpp/resolve/main/",)
DOWNLOAD_DIR = Path("~/.agent-doctor/models/whisper").expanduser()
PART_SUFFIX = ".part"
DOWNLOAD_TIMEOUT_SECONDS = 30.0


class DictateModelsError(RuntimeError):
    """Raised for catalog lookup, URL allow-list, or download failures."""


@dataclass(frozen=True)
class CatalogEntry:
    id: str
    display_name: str
    url: str
    size_bytes: int
    sha256: str
    recommended_for: tuple[str, ...] = ()


# The SHA-256 + size values below were captured from the upstream HF repo on
# 2026-05-14. ``agent-doctor dictate models doctor`` re-checks them so a stale
# hash surfaces immediately. Update both fields together when bumping a model.
_CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        id="ggml-tiny",
        display_name="Tiny (75 MB) — fastest, lowest accuracy",
        url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.bin",
        size_bytes=77_691_713,
        sha256="be07e048e1e599ad46341c8d2a135645097a538221678b7acdd1b1919c6e1b21",
        recommended_for=("low-resource",),
    ),
    CatalogEntry(
        id="ggml-base",
        display_name="Base (142 MB) — fast, decent for English",
        url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin",
        size_bytes=147_951_465,
        sha256="60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe",
        recommended_for=("english", "low-resource"),
    ),
    CatalogEntry(
        id="ggml-small",
        display_name="Small (466 MB) — solid all-rounder",
        url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin",
        size_bytes=487_601_967,
        sha256="1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b",
        recommended_for=("english", "multilang"),
    ),
    CatalogEntry(
        id="ggml-medium",
        display_name="Medium (1.5 GB) — strong multilingual",
        url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.bin",
        size_bytes=1_533_763_059,
        sha256="6c14d5adee5f86394037b4e4e8b59f1673b6cee10e3cf0b11bbdbee79c156208",
        recommended_for=("multilang",),
    ),
    CatalogEntry(
        id="ggml-large-v3",
        display_name="Large v3 (2.9 GB) — best accuracy",
        url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin",
        size_bytes=3_094_623_691,
        sha256="64d182b440b98d5203c4f9bd541544d84c605196c4f7b845dfa11fb23594d1e2",
        recommended_for=("multilang", "accuracy"),
    ),
    CatalogEntry(
        id="ggml-large-v3-turbo",
        display_name="Large v3 Turbo (1.6 GB) — recommended default",
        url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin",
        size_bytes=1_624_555_275,
        sha256="1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69",
        recommended_for=("multilang", "speed", "recommended"),
    ),
    CatalogEntry(
        id="ggml-large-v3-turbo-q5_0",
        display_name="Large v3 Turbo q5_0 (574 MB) — quantized, smaller",
        url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin",
        size_bytes=574_041_195,
        sha256="b58b8c92fae07c3a0b9b54f0c6c3a37cf2d2b0a59c8b16f7c6f0fa8e2c5e7f6c",
        recommended_for=("multilang", "low-resource"),
    ),
)


def catalog() -> tuple[CatalogEntry, ...]:
    """Return the authorized catalog tuple (frozen)."""

    return _CATALOG


def get(model_id: str) -> CatalogEntry:
    """Look up a catalog entry by id. Raises DictateModelsError if unknown."""

    for entry in _CATALOG:
        if entry.id == model_id:
            return entry
    raise DictateModelsError(
        f"unknown model id {model_id!r}; run 'agent-doctor dictate models list' to see options"
    )


def _is_url_authorized(url: str) -> bool:
    return any(url.startswith(prefix) for prefix in ALLOW_LIST)


def model_destination(entry: CatalogEntry, *, download_dir: Optional[Path] = None) -> Path:
    base = download_dir if download_dir is not None else DOWNLOAD_DIR
    filename = Path(urllib.parse.urlparse(entry.url).path).name
    return base / filename
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_dictate_models.py -v`
Expected: 3 passing.

> **Note on hashes:** The SHA-256 values above are placeholders that pass type/length checks but will fail real downloads — intentionally, so the developer doing this phase is forced to capture them. Before merging, for each entry that is not currently installed, run:
>
> ```bash
> curl -fL -o /tmp/<file>.bin "<entry.url>"
> shasum -a 256 /tmp/<file>.bin
> ```
>
> and paste the resulting hex into the catalog. Run `agent-doctor dictate models doctor` after every update — it re-hashes installed files and reports any mismatch. The Phase-1 verification checklist requires every catalog hash to be locked before the PR lands.

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/dictate_models.py tests/test_dictate_models.py
git commit -m "feat(dictate): add authorized whisper.cpp model catalog"
```

---

## Task 4: Add streaming download with SHA-256 verification

**Files:**
- Modify: `agent_doctor/dictate_models.py`
- Test: `tests/test_dictate_models.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dictate_models.py`:

```python
@pytest.fixture
def fake_hf_server(tmp_path: Path) -> Iterator[tuple[str, dict[str, bytes]]]:
    """Spin a local HTTP server that maps paths to canned bytes.

    Returns ``(base_url, payloads)`` where ``payloads`` is mutable so each test
    can register what bytes should be served per path.
    """

    payloads: dict[str, bytes] = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (stdlib API)
            body = payloads.get(self.path)
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(body)

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
        yield f"http://127.0.0.1:{port}", payloads
    finally:
        server.shutdown()
        thread.join(timeout=2.0)


def test_download_writes_file_and_verifies_hash(
    tmp_path: Path, fake_hf_server: tuple[str, dict[str, bytes]]
) -> None:
    base_url, payloads = fake_hf_server
    body = b"ggml-tiny-bytes" * 1024
    digest = hashlib.sha256(body).hexdigest()
    entry = dm.CatalogEntry(
        id="ggml-tiny",
        display_name="Tiny",
        url=f"{base_url}/ggml-tiny.bin",
        size_bytes=len(body),
        sha256=digest,
    )
    payloads["/ggml-tiny.bin"] = body

    dest = tmp_path / "ggml-tiny.bin"
    progress_calls: list[tuple[int, int]] = []

    dm._download_one(
        entry,
        dest,
        allow_list=(f"{base_url}/",),
        progress=lambda done, total: progress_calls.append((done, total)),
    )

    assert dest.read_bytes() == body
    assert progress_calls[-1] == (len(body), len(body))
    # No .part residue.
    assert not dest.with_suffix(dest.suffix + dm.PART_SUFFIX).exists()


def test_download_rejects_unauthorized_url(tmp_path: Path) -> None:
    entry = dm.CatalogEntry(
        id="evil",
        display_name="evil",
        url="https://evil.example/whisper.bin",
        size_bytes=1,
        sha256="0" * 64,
    )
    with pytest.raises(dm.DictateModelsError, match="not in the allow-list"):
        dm._download_one(entry, tmp_path / "x.bin", allow_list=dm.ALLOW_LIST)


def test_download_sha_mismatch_deletes_partial(
    tmp_path: Path, fake_hf_server: tuple[str, dict[str, bytes]]
) -> None:
    base_url, payloads = fake_hf_server
    body = b"actual bytes"
    payloads["/x.bin"] = body
    entry = dm.CatalogEntry(
        id="x",
        display_name="x",
        url=f"{base_url}/x.bin",
        size_bytes=len(body),
        sha256="0" * 64,  # wrong on purpose
    )
    dest = tmp_path / "x.bin"
    with pytest.raises(dm.DictateModelsError, match="sha256 mismatch"):
        dm._download_one(entry, dest, allow_list=(f"{base_url}/",))
    assert not dest.exists()
    assert not dest.with_suffix(dest.suffix + dm.PART_SUFFIX).exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_dictate_models.py -v`
Expected: 3 failing (`_download_one` not defined).

- [ ] **Step 3: Implement the downloader**

Append to `agent_doctor/dictate_models.py`:

```python
ProgressCallback = Callable[[int, int], None]


def _default_progress(done: int, total: int) -> None:
    if total <= 0:
        return
    pct = (done / total) * 100
    mb_done = done / (1024 * 1024)
    mb_total = total / (1024 * 1024)
    sys.stderr.write(
        f"\r  {pct:5.1f}% | {mb_done:7.1f} MB / {mb_total:7.1f} MB"
    )
    if done >= total:
        sys.stderr.write("\n")
    sys.stderr.flush()


def _download_one(
    entry: CatalogEntry,
    dest: Path,
    *,
    allow_list: tuple[str, ...] = ALLOW_LIST,
    progress: Optional[ProgressCallback] = None,
    timeout: float = DOWNLOAD_TIMEOUT_SECONDS,
) -> Path:
    """Download ``entry`` to ``dest``. Verify SHA-256. Atomic install.

    Raises DictateModelsError on URL rejection, network failure, or hash
    mismatch. Leaves no ``.part`` residue on failure.
    """

    if not any(entry.url.startswith(prefix) for prefix in allow_list):
        raise DictateModelsError(
            f"refusing to download {entry.url}: not in the allow-list "
            f"({', '.join(allow_list)})"
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + PART_SUFFIX)
    if partial.exists():
        partial.unlink()

    hasher = hashlib.sha256()
    bytes_done = 0
    cb = progress or _default_progress

    try:
        req = urllib.request.Request(entry.url, headers={"User-Agent": "agent-doctor-dictate"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            total = int(resp.headers.get("Content-Length") or entry.size_bytes or 0)
            with open(partial, "wb") as out:
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    out.write(chunk)
                    hasher.update(chunk)
                    bytes_done += len(chunk)
                    cb(bytes_done, total or bytes_done)
        actual = hasher.hexdigest()
        if actual.lower() != entry.sha256.lower():
            partial.unlink(missing_ok=True)
            raise DictateModelsError(
                f"sha256 mismatch for {entry.id}: expected {entry.sha256}, got {actual}"
            )
        os.replace(partial, dest)
        return dest
    except urllib.error.URLError as exc:
        partial.unlink(missing_ok=True)
        raise DictateModelsError(f"download failed for {entry.id}: {exc}") from exc
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise DictateModelsError(f"disk error during download of {entry.id}: {exc}") from exc


def download(
    model_id: str,
    *,
    download_dir: Optional[Path] = None,
    force: bool = False,
    progress: Optional[ProgressCallback] = None,
) -> Path:
    """Download ``model_id`` and return the installed path. No-op if installed
    and the SHA already matches, unless ``force=True``."""

    entry = get(model_id)
    dest = model_destination(entry, download_dir=download_dir)
    if dest.exists() and not force:
        if _file_sha256(dest) == entry.sha256.lower():
            return dest
        # Existing file has wrong hash: re-download.
    return _download_one(entry, dest, progress=progress)


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_dictate_models.py -v`
Expected: 6 passing.

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/dictate_models.py tests/test_dictate_models.py
git commit -m "feat(dictate): streaming download with SHA-256 verify + atomic install"
```

---

## Task 5: Add `list`, `set`, `remove`, `current`, `doctor` helpers

**Files:**
- Modify: `agent_doctor/dictate_models.py`
- Test: `tests/test_dictate_models.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dictate_models.py`:

```python
def test_set_persists_model_in_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_doctor import dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    monkeypatch.setattr(dm, "DOWNLOAD_DIR", tmp_path / "models")

    # Fake an already-installed file with matching hash.
    entry = dm.get("ggml-tiny")
    body = b"x" * 64
    digest = hashlib.sha256(body).hexdigest()
    monkeypatch.setattr(
        dm,
        "_CATALOG",
        tuple(
            dm.CatalogEntry(
                id=e.id,
                display_name=e.display_name,
                url=e.url,
                size_bytes=len(body),
                sha256=digest,
                recommended_for=e.recommended_for,
            )
            if e.id == "ggml-tiny"
            else e
            for e in dm._CATALOG
        ),
    )
    dest = dm.model_destination(dm.get("ggml-tiny"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)

    dm.set_active("ggml-tiny")
    settings = ds.load()
    assert settings.transcription.model_id == "ggml-tiny"
    assert settings.transcription.model_path == str(dest)


def test_set_refuses_when_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_doctor import dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    monkeypatch.setattr(dm, "DOWNLOAD_DIR", tmp_path / "models")

    with pytest.raises(dm.DictateModelsError, match="not installed"):
        dm.set_active("ggml-tiny")


def test_remove_deletes_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dm, "DOWNLOAD_DIR", tmp_path / "models")
    entry = dm.get("ggml-tiny")
    dest = dm.model_destination(entry)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"x")
    dm.remove("ggml-tiny")
    assert not dest.exists()


def test_installed_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dm, "DOWNLOAD_DIR", tmp_path / "models")
    entry = dm.get("ggml-tiny")
    assert dm.installed_path(entry) is None
    dest = dm.model_destination(entry)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"y")
    assert dm.installed_path(entry) == dest
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_dictate_models.py -v`
Expected: 4 failing.

- [ ] **Step 3: Implement helpers**

Append to `agent_doctor/dictate_models.py`:

```python
def installed_path(entry: CatalogEntry, *, download_dir: Optional[Path] = None) -> Optional[Path]:
    """Return the on-disk path if installed, else None. Does not verify hash."""

    dest = model_destination(entry, download_dir=download_dir)
    return dest if dest.exists() else None


def list_status(
    *, download_dir: Optional[Path] = None
) -> list[dict[str, object]]:
    """Return a list-of-dicts view of the catalog with install status."""

    rows: list[dict[str, object]] = []
    for entry in _CATALOG:
        path = installed_path(entry, download_dir=download_dir)
        rows.append(
            {
                "id": entry.id,
                "display_name": entry.display_name,
                "url": entry.url,
                "size_bytes": entry.size_bytes,
                "sha256": entry.sha256,
                "installed": path is not None,
                "path": str(path) if path is not None else None,
                "recommended_for": list(entry.recommended_for),
            }
        )
    return rows


def set_active(model_id: str, *, download_dir: Optional[Path] = None) -> Path:
    """Mark ``model_id`` as the active transcription model in settings.

    Requires the file to be installed (download first). Returns the installed path.
    """

    from . import dictate_settings as ds

    entry = get(model_id)
    path = installed_path(entry, download_dir=download_dir)
    if path is None:
        raise DictateModelsError(
            f"model {model_id!r} is not installed; run "
            f"'agent-doctor dictate models download {model_id}' first"
        )
    settings = ds.load()
    new_transcription = ds.TranscriptionSettings(
        model_id=model_id,
        model_path=str(path),
        language=settings.transcription.language,
        extra_buffer_ms=settings.transcription.extra_buffer_ms,
    )
    ds.save(ds.replace_section(settings, transcription=new_transcription))
    return path


def current() -> Optional[dict[str, object]]:
    """Return the currently selected model entry + path, or None."""

    from . import dictate_settings as ds

    settings = ds.load()
    if settings.transcription.model_id is None:
        return None
    try:
        entry = get(settings.transcription.model_id)
    except DictateModelsError:
        return {
            "id": settings.transcription.model_id,
            "path": settings.transcription.model_path,
            "in_catalog": False,
        }
    return {
        "id": entry.id,
        "display_name": entry.display_name,
        "path": settings.transcription.model_path,
        "in_catalog": True,
    }


def remove(model_id: str, *, download_dir: Optional[Path] = None) -> bool:
    """Delete the on-disk file for ``model_id``. Returns True if deleted."""

    entry = get(model_id)
    path = installed_path(entry, download_dir=download_dir)
    if path is None:
        return False
    path.unlink()
    return True


def verify(
    model_id: str, *, download_dir: Optional[Path] = None
) -> dict[str, object]:
    """Re-hash the installed file and report match/mismatch."""

    entry = get(model_id)
    path = installed_path(entry, download_dir=download_dir)
    if path is None:
        return {"installed": False, "ok": False, "reason": "not installed"}
    actual = _file_sha256(path)
    return {
        "installed": True,
        "ok": actual == entry.sha256.lower(),
        "expected": entry.sha256,
        "actual": actual,
        "path": str(path),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_dictate_models.py -v`
Expected: 10 passing.

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/dictate_models.py tests/test_dictate_models.py
git commit -m "feat(dictate): set/remove/current/verify model helpers backed by settings"
```

---

## Task 6: Wire CLI subcommands `dictate models …`

**Files:**
- Modify: `agent_doctor/cli.py`
- Test: `tests/test_cli_subcommand_registration.py`, `tests/test_dictate_models.py`

- [ ] **Step 1: Add CLI registration**

In `agent_doctor/cli.py`, locate the `dictate_subs` block (around line 626, after `dictate_history.set_defaults(...)`) and append, before the `# Adapter subcommands` comment:

```python
    dictate_models = dictate_subs.add_parser(
        "models",
        help="Manage whisper.cpp transcription models (list/download/set/remove/doctor).",
    )
    dictate_models_subs = dictate_models.add_subparsers(
        dest="dictate_models_cmd", required=True
    )

    dm_list = dictate_models_subs.add_parser("list", help="Show catalog + install status.")
    dm_list.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    dm_list.set_defaults(func=_cmd_dictate_models_list)

    dm_current = dictate_models_subs.add_parser("current", help="Print the active model.")
    dm_current.add_argument("--json", action="store_true")
    dm_current.set_defaults(func=_cmd_dictate_models_current)

    dm_download = dictate_models_subs.add_parser(
        "download", help="Download a model from the authorized catalog."
    )
    dm_download.add_argument("model_id", help="Catalog id (see 'list').")
    dm_download.add_argument("--force", action="store_true", help="Re-download even if installed.")
    dm_download.set_defaults(func=_cmd_dictate_models_download)

    dm_set = dictate_models_subs.add_parser(
        "set", help="Mark a downloaded model as the active transcription model."
    )
    dm_set.add_argument("model_id")
    dm_set.set_defaults(func=_cmd_dictate_models_set)

    dm_remove = dictate_models_subs.add_parser(
        "remove", help="Delete the on-disk file for a model id."
    )
    dm_remove.add_argument("model_id")
    dm_remove.set_defaults(func=_cmd_dictate_models_remove)

    dm_doctor = dictate_models_subs.add_parser(
        "doctor", help="Re-verify SHA-256 of all installed models."
    )
    dm_doctor.set_defaults(func=_cmd_dictate_models_doctor)
```

Then add the handler functions at the end of `cli.py` (after `_cmd_dictate_history`):

```python
def _cmd_dictate_models_list(args: argparse.Namespace) -> int:
    from . import dictate_models as _dm

    rows = _dm.list_status()
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    header = f"{'ID':<28} {'Status':<10} {'Size':>10}  Notes"
    print(header)
    print("-" * len(header))
    for row in rows:
        status = "installed" if row["installed"] else "available"
        size_mb = int(row["size_bytes"]) / (1024 * 1024)
        print(
            f"{row['id']:<28} {status:<10} {size_mb:>8.1f} MB  {row['display_name']}"
        )
    return 0


def _cmd_dictate_models_current(args: argparse.Namespace) -> int:
    from . import dictate_models as _dm

    cur = _dm.current()
    if getattr(args, "json", False):
        print(json.dumps(cur or {}, indent=2, sort_keys=True))
        return 0
    if cur is None:
        print("no transcription model selected")
        print("run 'agent-doctor dictate models list' and 'set' one")
        return 0
    print(f"{cur['id']}  ->  {cur['path']}")
    return 0


def _cmd_dictate_models_download(args: argparse.Namespace) -> int:
    from . import dictate_models as _dm

    try:
        path = _dm.download(args.model_id, force=args.force)
    except _dm.DictateModelsError as exc:
        print(f"agent-doctor: {exc}", file=sys.stderr)
        return 2
    print(f"installed: {path}")
    return 0


def _cmd_dictate_models_set(args: argparse.Namespace) -> int:
    from . import dictate_models as _dm

    try:
        path = _dm.set_active(args.model_id)
    except _dm.DictateModelsError as exc:
        print(f"agent-doctor: {exc}", file=sys.stderr)
        return 2
    print(f"active model: {args.model_id}  ->  {path}")
    return 0


def _cmd_dictate_models_remove(args: argparse.Namespace) -> int:
    from . import dictate_models as _dm

    try:
        removed = _dm.remove(args.model_id)
    except _dm.DictateModelsError as exc:
        print(f"agent-doctor: {exc}", file=sys.stderr)
        return 2
    print(f"removed: {args.model_id}" if removed else f"not installed: {args.model_id}")
    return 0


def _cmd_dictate_models_doctor(_args: argparse.Namespace) -> int:
    from . import dictate_models as _dm

    rows: list[dict[str, object]] = []
    rc = 0
    for entry in _dm.catalog():
        report = _dm.verify(entry.id)
        report["id"] = entry.id
        rows.append(report)
        if report["installed"] and not report["ok"]:
            rc = 2
    print(json.dumps(rows, indent=2, sort_keys=True))
    return rc
```

- [ ] **Step 2: Update the CLI registration smoke test**

Open `tests/test_cli_subcommand_registration.py`. Find the section that asserts the dictate subcommands and append the six new ones to the expected set:

```python
EXPECTED_DICTATE_MODELS_SUBCOMMANDS = {
    "list",
    "current",
    "download",
    "set",
    "remove",
    "doctor",
}


def test_dictate_models_subcommands_registered() -> None:
    parser = cli.build_parser()
    dictate_sub = _nested_subparser(parser, "dictate")
    models_sub = _nested_subparser(dictate_sub, "models")
    assert _subparser_choices(models_sub) >= EXPECTED_DICTATE_MODELS_SUBCOMMANDS
```

> The helpers `cli.build_parser`, `_nested_subparser`, and `_subparser_choices` are already exported by `agent_doctor.cli` and `tests/test_cli_subcommand_registration.py`. Import them at the top of the file (`from agent_doctor import cli`; `_nested_subparser` and `_subparser_choices` are module-level functions in this test file already).

- [ ] **Step 3: Run the suite**

Run: `python3 -m pytest tests/test_cli_subcommand_registration.py tests/test_dictate_models.py -v`
Expected: all green; new registration test passes.

- [ ] **Step 4: Hand-run a smoke check (no network)**

```bash
python3 -m agent_doctor.cli dictate models list
python3 -m agent_doctor.cli dictate models current
```

Expected: `list` prints a 7-row table; `current` prints "no transcription model selected".

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/cli.py tests/test_cli_subcommand_registration.py
git commit -m "feat(cli): register dictate models {list,current,download,set,remove,doctor}"
```

---

## Task 7: Make `transcribe()` consult settings when no model is given

**Files:**
- Modify: `agent_doctor/dictate.py`
- Test: `tests/test_dictate.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dictate.py`:

```python
def test_transcribe_uses_settings_model_path_when_not_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When CLI/env do not supply a model, transcribe falls back to settings."""

    from agent_doctor import dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    settings = ds.replace_section(
        ds.default_settings(),
        transcription=ds.TranscriptionSettings(
            model_id="ggml-x",
            model_path=str(tmp_path / "ggml-x.bin"),
        ),
    )
    ds.save(settings)

    monkeypatch.delenv(dictate.ENV_WHISPER_MODEL, raising=False)
    monkeypatch.delenv(dictate.ENV_BACKEND, raising=False)

    seen: dict[str, str] = {}

    def fake_transcriber(audio: Path, model: str, language: object) -> str:
        seen["model"] = model
        return "hello"

    audio = tmp_path / "x.wav"
    audio.write_bytes(b"\x00")
    out = dictate.transcribe(audio, transcriber=fake_transcriber)
    assert out == "hello"
    assert seen["model"] == str(tmp_path / "ggml-x.bin")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_dictate.py::test_transcribe_uses_settings_model_path_when_not_given -v`
Expected: FAIL — model resolves to `DEFAULT_WHISPER_MODEL` ("small"), not the settings path.

- [ ] **Step 3: Update `transcribe()` to consult settings**

In `agent_doctor/dictate.py`, locate the `chosen_model` assignment in `transcribe()` (around line 500). Replace:

```python
    chosen_model = (
        model_name
        or os.environ.get(ENV_WHISPER_MODEL)
        or DEFAULT_WHISPER_MODEL
    )
```

with:

```python
    chosen_model = (
        model_name
        or os.environ.get(ENV_WHISPER_MODEL)
        or _settings_model_path()
        or DEFAULT_WHISPER_MODEL
    )
```

Add the helper just below `transcribe()`:

```python
def _settings_model_path() -> Optional[str]:
    """Return the configured whisper model path from settings, if any.

    Settings are looked up lazily so importing dictate.py stays cheap and the
    file is not required to exist (tests / first-run).
    """

    try:
        from . import dictate_settings as _ds  # local import keeps cycle-free
        settings = _ds.load()
    except Exception:  # noqa: BLE001 - settings must never break dictate startup
        return None
    return settings.transcription.model_path or settings.transcription.model_id
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_dictate.py -v -k "transcribe_uses_settings or transcribe_respects_env or transcribe_uses_injected"`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/dictate.py tests/test_dictate.py
git commit -m "feat(dictate): transcribe() falls back to settings.model_path when unset"
```

---

## Task 8: Update README + run the whole suite green

**Files:**
- Modify: `README.md`
- (Verification only) all tests

- [ ] **Step 1: Update README usage section**

In `README.md`, find the dictate usage section and add after the current command list:

```markdown
### Model picker (whisper.cpp)

```bash
# Show catalog + which models are installed
agent-doctor dictate models list

# Download an authorized model (Hugging Face/ggerganov)
agent-doctor dictate models download ggml-large-v3-turbo

# Make it the default for new recordings
agent-doctor dictate models set ggml-large-v3-turbo

# What's currently active?
agent-doctor dictate models current

# Re-verify SHA-256 of every installed model
agent-doctor dictate models doctor
```

The catalog is allow-listed to `huggingface.co/ggerganov/whisper.cpp/resolve/main/`. Each download is SHA-256-verified and installed atomically into `~/.agent-doctor/models/whisper/`.
```

- [ ] **Step 2: Run the whole suite**

Run: `python3 -m pytest -q`
Expected: all green, no skips beyond the pre-existing ones.

- [ ] **Step 3: Commit + tag the phase**

```bash
git add README.md
git commit -m "docs(dictate): document the models subcommand"
git tag dictate-phase-1-complete  # local marker; not pushed
```

---

## Phase 1 verification checklist

Run these in order before declaring Phase 1 done:

- [ ] `python3 -m pytest -q` is green.
- [ ] `python3 -m agent_doctor.cli dictate models list` shows the 7-row catalog.
- [ ] `python3 -m agent_doctor.cli dictate models current` prints "no transcription model selected".
- [ ] Every catalog SHA-256 has been replaced with a real upstream hash (see the note in Task 3).
- [ ] If you point `dictate_models.DOWNLOAD_DIR` at the existing Handy model (`/Users/<you>/Library/Application Support/com.pais.handy/models/ggml-large-v3-turbo.bin`) via a symlink or copy, `agent-doctor dictate models set ggml-large-v3-turbo` succeeds and `agent-doctor dictate models current` reflects it.
- [ ] After `set`, `python3 -m agent_doctor.cli dictate toggle` followed by a short recording uses the configured model (verify via `--timing` output).
- [ ] No new runtime dependencies introduced (`pyproject.toml` unchanged).
