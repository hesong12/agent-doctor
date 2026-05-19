"""Voice → optimized-prompt clipboard pipeline.

The ``dictate`` subcommand provides a Handy-style push-to-talk surface, but
instead of inserting raw transcription into the focused text field, it routes
the transcript through an OpenAI-compatible chat completion endpoint to
produce a *cleaned, structured prompt* that the user can paste into any AI
application (Cursor, Claude Code, ChatGPT, Claude Desktop, …).

Pipeline
========

1. ``start``  — spawn ``rec`` (sox) writing a 16 kHz mono WAV to a temp file;
   persist state (PID, file path, mode, started_at) so a later ``stop`` or
   ``toggle`` can find it.
2. ``stop``   — SIGTERM the recorder, wait for clean exit, transcribe the WAV
   with ``faster-whisper``, route the transcript through the LLM enhancer with
   the chosen mode prompt, write the result to the macOS clipboard, and post a
   user notification.
3. ``toggle`` — convenience: stop if recording, else start with the supplied
   mode/model overrides.
4. ``status`` — emit the current state as JSON.

Design constraints
==================

- **No new required dependencies.** ``faster-whisper``, ``sounddevice``, and
  ``soundfile`` are pulled in by the optional ``dictate`` extra and lazy-imported
  inside the transcription step. Recording uses ``sox``/``rec`` (or ffmpeg as
  fallback) via ``subprocess``, so the core CLI keeps the project's
  zero-required-dependency posture from ``pyproject.toml``.
- **Network-optional.** The LLM enhancer is opt-in; ``--no-enhance`` and the
  ``raw`` mode both write the transcript verbatim. When the configured LLM
  endpoint is unreachable the pipeline degrades to the raw transcript with a
  warning instead of failing the whole run.
- **macOS-first.** Clipboard uses ``pbcopy`` and notifications use
  ``osascript``. The module is import-safe on Linux for unit tests but the
  user-facing commands assume Darwin (matching the v1 desktop scope).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# ----------------------------------------------------------------------------- 
# Constants / configuration                                                     
# -----------------------------------------------------------------------------

DEFAULT_LLM_URL = "http://localhost:8080/v1/chat/completions"
DEFAULT_LLM_MODEL = "ds4"
DEFAULT_WHISPER_MODEL = "small"
DEFAULT_MODE = "optimize"
SUPPORTED_MODES = ("optimize", "chat", "coding", "research", "raw")
DEPRECATED_MODES = ("chat", "coding", "research")

# Whisper backend choices. ``auto`` inspects ``model_name``: a path ending in
# ``.bin`` or ``.gguf`` (or any existing local file path) routes to
# ``whisper-cpp``; everything else routes to ``faster-whisper``.
SUPPORTED_BACKENDS = ("auto", "faster-whisper", "whisper-cpp")
DEFAULT_BACKEND = "auto"

ENV_LLM_URL = "AGENT_DOCTOR_DICTATE_LLM_URL"
ENV_LLM_MODEL = "AGENT_DOCTOR_DICTATE_LLM_MODEL"
ENV_LLM_KEY = "AGENT_DOCTOR_DICTATE_LLM_KEY"
ENV_WHISPER_MODEL = "AGENT_DOCTOR_DICTATE_WHISPER_MODEL"
ENV_BACKEND = "AGENT_DOCTOR_DICTATE_BACKEND"  # auto | faster-whisper | whisper-cpp
ENV_STATE_DIR = "AGENT_DOCTOR_DICTATE_STATE_DIR"
ENV_RECORDER = "AGENT_DOCTOR_DICTATE_RECORDER"  # "sox" | "ffmpeg"
ENV_BUFFER_MS = "AGENT_DOCTOR_DICTATE_BUFFER_MS"           # extra recording tail
ENV_BEEP = "AGENT_DOCTOR_DICTATE_BEEP"                     # "1" -> play sounds
ENV_HISTORY_LIMIT = "AGENT_DOCTOR_DICTATE_HISTORY_LIMIT"   # rows to retain

# Extra audio captured AFTER ``dictate stop`` is invoked, in milliseconds.
# Inspired by Handy's ``extra_recording_buffer_ms`` setting. The user often
# releases the hotkey while still finishing their final syllable; without a
# tail buffer that syllable gets cut. 150 ms is enough for a closing
# consonant or short word without making the user wait perceptibly.
DEFAULT_BUFFER_MS = 150

# macOS built-in sound files used for start/stop audio feedback.
DEFAULT_START_SOUND = "/System/Library/Sounds/Pop.aiff"
DEFAULT_DONE_SOUND = "/System/Library/Sounds/Glass.aiff"
DEFAULT_FAIL_SOUND = "/System/Library/Sounds/Basso.aiff"

# History database. SQLite file under the same state dir as the recording
# state. We keep the most recent N rows; older rows are pruned in the same
# transaction as the insert so the file never grows unbounded.
DEFAULT_HISTORY_LIMIT = 100
HISTORY_FILENAME = "dictate-history.sqlite3"

# How long ``stop`` waits for the recorder to exit cleanly after SIGTERM
# before escalating to SIGKILL.
RECORDER_TERM_GRACE_SECONDS = 3.0


class DictateError(RuntimeError):
    """User-visible error raised by the dictate pipeline."""


# ----------------------------------------------------------------------------- 
# Mode prompts                                                                  
# -----------------------------------------------------------------------------

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


def is_raw_mode(mode: str) -> bool:
    return mode == "raw"


# ----------------------------------------------------------------------------- 
# State management                                                              
# -----------------------------------------------------------------------------


@dataclass
class DictateState:
    """Filesystem-persisted state for an in-flight recording."""

    pid: int
    audio_path: str
    mode: str
    started_at: float
    recorder: str
    extras: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pid": self.pid,
            "audio_path": self.audio_path,
            "mode": self.mode,
            "started_at": self.started_at,
            "recorder": self.recorder,
            "extras": dict(self.extras),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "DictateState":
        return cls(
            pid=int(payload["pid"]),
            audio_path=str(payload["audio_path"]),
            mode=str(payload.get("mode", DEFAULT_MODE)),
            started_at=float(payload.get("started_at", 0.0)),
            recorder=str(payload.get("recorder", "sox")),
            extras=dict(payload.get("extras") or {}),
        )


def default_state_dir() -> Path:
    override = os.environ.get(ENV_STATE_DIR)
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "agent-doctor"
    return Path.home() / ".config" / "agent-doctor"


def state_file(state_dir: Optional[Path] = None) -> Path:
    base = state_dir if state_dir is not None else default_state_dir()
    return base / "dictate.state.json"


def write_state(state: DictateState, state_dir: Optional[Path] = None) -> Path:
    path = state_file(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state.to_dict(), indent=2, sort_keys=True))
    tmp.replace(path)
    return path


def read_state(state_dir: Optional[Path] = None) -> Optional[DictateState]:
    path = state_file(state_dir)
    if not path.exists():
        return None
    try:
        return DictateState.from_dict(json.loads(path.read_text()))
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        raise DictateError(f"corrupt dictate state file at {path}: {exc}") from exc


def clear_state(state_dir: Optional[Path] = None) -> None:
    path = state_file(state_dir)
    if path.exists():
        path.unlink()


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but is owned by another user; treat as alive.
        return True
    return True


# ----------------------------------------------------------------------------- 
# Recording                                                                     
# -----------------------------------------------------------------------------


def _detect_recorder() -> str:
    override = os.environ.get(ENV_RECORDER)
    if override:
        binary = "rec" if override == "sox" else override
        if not shutil.which(binary):
            raise DictateError(
                f"recorder '{override}' specified via {ENV_RECORDER} but "
                f"'{binary}' was not found on PATH"
            )
        return override
    if shutil.which("rec"):
        return "sox"
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    raise DictateError(
        "no audio recorder found; install sox (brew install sox) or ffmpeg"
    )


def _build_recorder_argv(recorder: str, audio_path: Path) -> List[str]:
    if recorder == "sox":
        return [
            "rec",
            "-q",
            "-c",
            "1",
            "-r",
            "16000",
            "-b",
            "16",
            str(audio_path),
        ]
    if recorder == "ffmpeg":
        # ``:0`` selects the default audio input on macOS via AVFoundation.
        # ``-nostdin`` keeps ffmpeg quiet about its interactive prompts.
        return [
            "ffmpeg",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "avfoundation",
            "-i",
            ":0",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(audio_path),
        ]
    raise DictateError(f"unsupported recorder '{recorder}'")


def start_recording(
    *,
    mode: str = DEFAULT_MODE,
    state_dir: Optional[Path] = None,
    audio_dir: Optional[Path] = None,
    recorder: Optional[str] = None,
    spawn: Callable[[List[str]], "subprocess.Popen[bytes]"] = None,  # type: ignore[assignment]
) -> DictateState:
    """Start a recording session.

    Raises :class:`DictateError` if a recording is already in flight or the
    requested recorder binary is missing. ``spawn`` is overridable for tests so
    a fake Popen can be substituted without touching real audio devices.
    """

    if mode not in SUPPORTED_MODES:
        raise DictateError(
            f"unknown mode '{mode}'; expected one of {', '.join(SUPPORTED_MODES)}"
        )

    existing = read_state(state_dir)
    if existing is not None and is_pid_alive(existing.pid):
        raise DictateError(
            f"a dictate recording is already running (pid={existing.pid}, "
            f"mode={existing.mode}); run 'agent-doctor dictate stop' first"
        )
    if existing is not None:
        # Stale state — recorder died without cleanup.
        clear_state(state_dir)

    chosen = recorder or _detect_recorder()
    audio_root = (
        audio_dir
        if audio_dir is not None
        else Path(tempfile.gettempdir())
    )
    audio_root.mkdir(parents=True, exist_ok=True)
    audio_path = audio_root / f"agent-doctor-dictate-{uuid.uuid4().hex}.wav"
    argv = _build_recorder_argv(chosen, audio_path)

    _spawn = spawn or _default_spawn
    try:
        proc = _spawn(argv)
    except FileNotFoundError as exc:
        raise DictateError(
            f"recorder binary '{argv[0]}' not found or not executable: {exc}"
        ) from exc

    state = DictateState(
        pid=proc.pid,
        audio_path=str(audio_path),
        mode=mode,
        started_at=time.time(),
        recorder=chosen,
        extras={"argv": argv},
    )
    write_state(state, state_dir)
    return state


def _default_spawn(argv: List[str]) -> "subprocess.Popen[bytes]":
    return subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def stop_recording(
    *,
    state_dir: Optional[Path] = None,
    terminator: Callable[[int, int], None] = None,  # type: ignore[assignment]
    waiter: Callable[[int, float], bool] = None,  # type: ignore[assignment]
) -> Path:
    """Terminate the running recorder and return the path to the captured WAV.

    Does not clear the state file — :func:`run_pipeline` clears it after
    transcription succeeds so a crash mid-transcription leaves the audio on
    disk for recovery.
    """

    state = read_state(state_dir)
    if state is None:
        raise DictateError("no dictate recording is in flight")

    audio_path = Path(state.audio_path)
    _term = terminator or _default_terminate
    _wait = waiter or _default_wait_for_exit

    if is_pid_alive(state.pid):
        _term(state.pid, signal.SIGTERM)
        if not _wait(state.pid, RECORDER_TERM_GRACE_SECONDS):
            _term(state.pid, signal.SIGKILL)
            _wait(state.pid, RECORDER_TERM_GRACE_SECONDS)
    if not audio_path.exists() or audio_path.stat().st_size == 0:
        raise DictateError(
            f"recorder exited without writing audio to {audio_path}"
        )
    return audio_path


def _default_terminate(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return


def _default_wait_for_exit(pid: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_pid_alive(pid):
            return True
        time.sleep(0.05)
    return not is_pid_alive(pid)


# ----------------------------------------------------------------------------- 
# Transcription                                                                 
# -----------------------------------------------------------------------------


def detect_backend(model_name: Optional[str]) -> str:
    """Choose a whisper backend based on the model identifier.

    Heuristic:
    - ``model_name`` is a path with suffix ``.bin`` or ``.gguf``       -> whisper-cpp
    - ``model_name`` contains a path separator AND exists on disk      -> whisper-cpp
    - everything else (faster-whisper size aliases like ``small`` /
      ``large-v3-turbo``, or HF repo ids)                              -> faster-whisper

    Returns one of the concrete backend names in ``SUPPORTED_BACKENDS``
    excluding ``"auto"``.
    """

    if not model_name:
        return "faster-whisper"
    suffix = Path(model_name).suffix.lower()
    if suffix in (".bin", ".gguf"):
        return "whisper-cpp"
    if ("/" in model_name or os.sep in model_name) and Path(model_name).expanduser().exists():
        return "whisper-cpp"
    return "faster-whisper"


def transcribe(
    audio_path: Path,
    *,
    model_name: Optional[str] = None,
    backend: Optional[str] = None,
    language: Optional[str] = None,
    transcriber: Optional[Callable[[Path, str, Optional[str]], str]] = None,
) -> str:
    """Transcribe ``audio_path`` with the selected whisper backend.

    Backend selection precedence:
        explicit ``backend=`` kwarg
        > ``AGENT_DOCTOR_DICTATE_BACKEND`` env var
        > ``"auto"``
    With ``"auto"`` we route to ``whisper-cpp`` for local GGML / GGUF model
    files (e.g. Handy's ``ggml-large-v3-turbo.bin``) and to
    ``faster-whisper`` for size aliases or HF repo ids.

    ``transcriber`` is overridable for tests: if provided it bypasses backend
    selection entirely. This preserves the dependency-injection contract that
    the rest of the test suite relies on.
    """

    chosen_model = resolve_whisper_model(model_name)

    if transcriber is not None:
        text = transcriber(audio_path, chosen_model, language)
        return text.strip()

    chosen_backend = (
        backend
        or os.environ.get(ENV_BACKEND)
        or DEFAULT_BACKEND
    )
    if chosen_backend not in SUPPORTED_BACKENDS:
        raise DictateError(
            f"unknown whisper backend {chosen_backend!r}; expected one of "
            f"{', '.join(SUPPORTED_BACKENDS)}"
        )
    if chosen_backend == "auto":
        chosen_backend = detect_backend(chosen_model)

    if chosen_backend == "whisper-cpp":
        text = _default_transcribe_whisper_cpp(audio_path, chosen_model, language)
    elif chosen_backend == "faster-whisper":
        text = _default_transcribe(audio_path, chosen_model, language)
    else:  # pragma: no cover - unreachable; validated above
        raise DictateError(f"backend {chosen_backend!r} not implemented")
    return text.strip()


def resolve_whisper_model(cli_value: Optional[str]) -> str:
    """Return the effective whisper model name with the canonical precedence.

    Order:
        1. ``cli_value`` (e.g. ``args.whisper_model`` from argparse)
        2. ``AGENT_DOCTOR_DICTATE_WHISPER_MODEL`` env var
        3. ``settings.transcription.model_path`` / ``model_id`` written by
           ``dictate models set``
        4. :data:`DEFAULT_WHISPER_MODEL`

    Shared with :func:`transcribe` so the daemon-spawned ``dictate stop`` path
    (which records history metadata before calling ``transcribe``) sees the
    same effective model as the transcription itself. Previously the CLI
    skipped step (3), so a user who ran ``dictate models set ggml-large.bin``
    would still get history rows tagged ``"small"`` and — worse — whisper-cpp
    backend selection that should have routed to the GGML file was silently
    bypassed because the model name handed to :func:`transcribe` was the
    faster-whisper alias.
    """

    return (
        cli_value
        or os.environ.get(ENV_WHISPER_MODEL)
        or _settings_model_path()
        or DEFAULT_WHISPER_MODEL
    )


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


def _default_transcribe(
    audio_path: Path,
    model_name: str,
    language: Optional[str],
) -> str:
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised only when extras missing
        raise DictateError(
            "faster-whisper is not installed; install the dictate extra:\n"
            "  pipx inject agent-doctor 'agent-doctor[dictate]'\n"
            "or:\n"
            "  pip install 'agent-doctor[dictate]'"
        ) from exc

    # ``int8`` is the best CPU / Apple-Silicon trade-off for ctranslate2: on
    # M-series Macs the float16 path falls back to float32 (ctranslate2 does
    # not yet implement efficient fp16 on Apple Metal), so explicit int8 is
    # both faster and uses less memory. Callers who need higher precision can
    # subclass _default_transcribe or pass a custom ``transcriber`` callable.
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, _info = model.transcribe(
        str(audio_path),
        language=language,
        vad_filter=True,
    )
    return "".join(segment.text for segment in segments)


def _default_transcribe_whisper_cpp(
    audio_path: Path,
    model_name: str,
    language: Optional[str],
) -> str:
    try:
        from pywhispercpp.model import Model  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised only when extras missing
        raise DictateError(
            "pywhispercpp is not installed; install the dictate-cpp extra:\n"
            "  pipx inject agent-doctor pywhispercpp\n"
            "or:\n"
            "  pip install 'agent-doctor[dictate-cpp]'\n"
            "(pywhispercpp builds whisper.cpp from source; Xcode CLT required on macOS)"
        ) from exc

    model_path = str(Path(model_name).expanduser())
    n_threads = os.cpu_count() or 4

    # whisper.cpp's C-level printf logging is verbose (Metal init, model
    # metadata) and bypasses Python's logging. We previously wrapped both
    # Model() load and transcribe() in _maybe_suppress_native_output, which
    # dup2's /dev/null over fds 1/2. On Apple Silicon that collides with
    # whisper.cpp's Metal init (the framework opens diagnostic descriptors
    # that don't survive the dup2) and crashes the process with SIGSEGV
    # inside ggml_metal_init. We accept slightly noisier logs in exchange
    # for not crashing the dictate pipeline on the daemon path that ships
    # by default. AGENT_DOCTOR_DICTATE_DEBUG remains a no-op for symmetry
    # with the rest of the codebase; the engine prints regardless.
    model = Model(model_path, n_threads=n_threads, print_progress=False)
    kwargs: Dict[str, Any] = {}
    if language:
        kwargs["language"] = language
    segments = model.transcribe(str(audio_path), **kwargs)
    return "".join(segment.text for segment in segments)


@contextmanager
def _maybe_suppress_native_output(*, suppress: bool):
    """Redirect fds 1/2 to /dev/null while ``suppress`` is True.

    Required because whisper.cpp logs via C ``printf``, which Python's
    ``contextlib.redirect_stdout`` cannot intercept. Restore on exit even
    on exception so the user's terminal does not stay redirected.

    Robustness notes:
    - Python's ``sys.stdout`` / ``sys.stderr`` are line-buffered to a tty
      and block-buffered otherwise. Any pending Python output must be
      flushed *before* we redirect fd 1/2 so it actually reaches the
      user's terminal instead of being silently flushed into /dev/null
      after the dup2.
    - Resources are acquired in a try-pyramid so a failing ``os.dup``
      partway through does not leak the descriptors we already opened.
    """

    if not suppress:
        yield
        return

    # Flush Python-level buffers so prior output is not pulled into the
    # redirection window and lost.
    try:
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 - defensive; flushing must not break the path
        pass
    try:
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass

    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        saved_stdout = os.dup(1)
        try:
            saved_stderr = os.dup(2)
            try:
                os.dup2(devnull_fd, 1)
                os.dup2(devnull_fd, 2)
                try:
                    yield
                finally:
                    # Restore in reverse order; on macOS dup2 is atomic so
                    # this leaves no observable in-between state.
                    os.dup2(saved_stdout, 1)
                    os.dup2(saved_stderr, 2)
            finally:
                os.close(saved_stderr)
        finally:
            os.close(saved_stdout)
    finally:
        os.close(devnull_fd)


# ----------------------------------------------------------------------------- 
# LLM enhancement                                                               
# -----------------------------------------------------------------------------


@dataclass
class LLMConfig:
    url: str = DEFAULT_LLM_URL
    model: str = DEFAULT_LLM_MODEL
    api_key: Optional[str] = None
    timeout: float = 30.0
    temperature: float = 0.2


def llm_config_from_env(
    *,
    url: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
) -> LLMConfig:
    """Backwards-compatible shim around :func:`dictate_llm.llm_config`."""

    from . import dictate_llm as _dl
    return _dl.llm_config(url=url, model=model, api_key=api_key)


def enhance_prompt(
    transcript: str,
    *,
    mode: str = DEFAULT_MODE,
    config: Optional[LLMConfig] = None,
    caller: Optional[Callable[[LLMConfig, List[Dict[str, str]]], str]] = None,
) -> str:
    """Send ``transcript`` through the LLM enhancer and return the rewritten prompt.

    ``caller`` is overridable for tests. In production we use stdlib ``urllib``
    so no extra dependency is required.
    """

    if is_raw_mode(mode):
        return transcript.strip()
    if not transcript.strip():
        return ""

    cfg = config or llm_config_from_env()
    system = mode_system_prompt(mode)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": transcript.strip()},
    ]
    fn = caller or _default_llm_call
    return fn(cfg, messages).strip()


def _default_llm_call(cfg: LLMConfig, messages: List[Dict[str, str]]) -> str:
    # Lazy stdlib import to keep module import cheap.
    import urllib.error
    import urllib.request

    payload = {
        "model": cfg.model,
        "messages": messages,
        "temperature": cfg.temperature,
        "stream": False,
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"

    request = urllib.request.Request(cfg.url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=cfg.timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # Surface the HTTP status and the response body so users can diagnose
        # configuration issues (e.g. 401 wrong API key, 404 wrong path,
        # 422 bad model name) instead of seeing a generic "unreachable".
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - best-effort, never let logging break the error path
            err_body = ""
        snippet = err_body.strip().replace("\n", " ")[:400]
        raise DictateError(
            f"LLM enhancement endpoint {cfg.url} returned HTTP {exc.code} {exc.reason}"
            + (f": {snippet}" if snippet else "")
        ) from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise DictateError(
            f"LLM enhancement endpoint {cfg.url} is unreachable: {exc}"
        ) from exc

    try:
        parsed = json.loads(raw)
        return parsed["choices"][0]["message"]["content"]
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise DictateError(
            f"unexpected LLM response shape from {cfg.url}: {raw[:200]}"
        ) from exc


# ----------------------------------------------------------------------------- 
# Clipboard + notification                                                      
# -----------------------------------------------------------------------------


def copy_to_clipboard(
    text: str,
    *,
    runner: Callable[[List[str], bytes], int] = None,  # type: ignore[assignment]
) -> None:
    """Write ``text`` to the macOS clipboard via ``pbcopy``."""

    fn = runner or _default_pbcopy
    rc = fn(["pbcopy"], text.encode("utf-8"))
    if rc != 0:
        raise DictateError(f"pbcopy exited with status {rc}")


def _default_pbcopy(argv: List[str], data: bytes) -> int:
    if not shutil.which(argv[0]):
        raise DictateError(
            f"{argv[0]} not found on PATH; clipboard copy is macOS-only"
        )
    proc = subprocess.run(argv, input=data, check=False)
    return proc.returncode


def notify(
    title: str,
    message: str,
    *,
    runner: Callable[[List[str]], int] = None,  # type: ignore[assignment]
) -> None:
    """Post a macOS notification via osascript. No-op on non-Darwin."""

    if sys.platform != "darwin":
        return
    if not shutil.which("osascript"):
        return
    fn = runner or _default_osascript
    # AppleScript uses backslash as escape char; escape both backslashes
    # and double quotes so file paths and LaTeX-style content do not break the
    # outer 'display notification "..."' literal.
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    safe_message = message.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{safe_message}" with title "{safe_title}"'
    fn(["osascript", "-e", script])


def _default_osascript(argv: List[str]) -> int:
    proc = subprocess.run(argv, check=False)
    return proc.returncode


# ----------------------------------------------------------------------------- 
# Tail buffer (extra recording after stop)                                      
# -----------------------------------------------------------------------------


def resolve_buffer_ms(cli_value: Optional[int]) -> int:
    """Return the active extra-recording-buffer in milliseconds.

    Precedence: explicit CLI value > env var > default. Negative values are
    clamped to 0 (a negative buffer would shorten the recording, which is
    never what the user wants).
    """

    if cli_value is not None:
        value = cli_value
    else:
        env = os.environ.get(ENV_BUFFER_MS)
        if env is not None and env.strip():
            try:
                value = int(env)
            except ValueError as exc:
                raise DictateError(
                    f"{ENV_BUFFER_MS}={env!r} is not a valid integer"
                ) from exc
        else:
            value = DEFAULT_BUFFER_MS
    return max(0, value)


def maybe_sleep_for_buffer(buffer_ms: int, sleeper: Callable[[float], None] = time.sleep) -> None:
    """Sleep for ``buffer_ms`` to let the recorder capture extra tail audio.

    Factored out so tests can substitute a fake sleeper and observe the call
    without actually blocking. We accept the buffer in ms (caller-facing
    unit) and convert to seconds for ``time.sleep``.
    """

    if buffer_ms <= 0:
        return
    sleeper(buffer_ms / 1000.0)


# ----------------------------------------------------------------------------- 
# Audio feedback (optional start/done/fail chime)                               
# -----------------------------------------------------------------------------


def beep_enabled(cli_flag: Optional[bool]) -> bool:
    """Return whether to play audio feedback. Precedence: CLI > env > off."""

    if cli_flag is True:
        return True
    if cli_flag is False:
        # Explicit --no-beep -> override env.
        return False
    raw = os.environ.get(ENV_BEEP, "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def play_sound(
    path: str,
    *,
    runner: Optional[Callable[[List[str]], int]] = None,
) -> None:
    """Fire-and-forget play of a system sound via ``afplay`` on macOS.

    The ``runner`` seam keeps tests deterministic: they assert the argv
    instead of actually invoking the audio subsystem. On non-Darwin /
    missing ``afplay`` the call is a no-op so the import is safe on Linux
    CI runners.
    """

    if not path:
        return
    if sys.platform != "darwin":
        return
    if not shutil.which("afplay"):
        return
    if runner is None:
        runner = _default_afplay
    try:
        runner(["afplay", path])
    except Exception:  # noqa: BLE001 - audio feedback failures must never block dictate
        pass


def _default_afplay(argv: List[str]) -> int:
    # Spawn detached so the parent does not block on playback.
    subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return 0


# ----------------------------------------------------------------------------- 
# History (SQLite)                                                              
# -----------------------------------------------------------------------------


def history_path(state_dir: Optional[Path] = None) -> Path:
    base = state_dir if state_dir is not None else default_state_dir()
    return base / HISTORY_FILENAME


def _ensure_history_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transcripts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              REAL NOT NULL,
            mode            TEXT NOT NULL,
            backend         TEXT,
            whisper_model   TEXT,
            language        TEXT,
            transcript      TEXT NOT NULL,
            prompt          TEXT NOT NULL,
            enhanced        INTEGER NOT NULL,
            enhancer_failed INTEGER NOT NULL DEFAULT 0,
            audio_path      TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS transcripts_ts_idx ON transcripts(ts DESC)")


def _resolve_history_limit(override: Optional[int]) -> int:
    if override is not None:
        return max(0, override)
    raw = os.environ.get(ENV_HISTORY_LIMIT)
    if raw is None or not raw.strip():
        return DEFAULT_HISTORY_LIMIT
    try:
        return max(0, int(raw))
    except ValueError as exc:
        raise DictateError(
            f"{ENV_HISTORY_LIMIT}={raw!r} is not a valid integer"
        ) from exc


def record_history(
    *,
    transcript: str,
    prompt: str,
    mode: str,
    enhanced: bool,
    enhancer_failed: bool = False,
    audio_path: Optional[Path] = None,
    backend: Optional[str] = None,
    whisper_model: Optional[str] = None,
    language: Optional[str] = None,
    state_dir: Optional[Path] = None,
    retention_limit: Optional[int] = None,
    clock: Callable[[], float] = time.time,
) -> int:
    """Insert one transcript into the history DB and prune older rows past
    the retention limit. Returns the inserted row id.

    Best-effort: any sqlite error is wrapped in a ``DictateError`` so the
    caller can decide whether to surface or swallow it. We do NOT delete the
    audio file here; that is the CLI's responsibility.
    """

    limit = _resolve_history_limit(retention_limit)
    if limit == 0:
        # Opt-out: retention=0 means "do not record". The PR documents
        # AGENT_DOCTOR_DICTATE_HISTORY_LIMIT=0 as the global disable; honour
        # that here so we do not insert and then immediately prune.
        return 0
    path = history_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(str(path)) as conn:
            _ensure_history_schema(conn)
            cursor = conn.execute(
                """
                INSERT INTO transcripts (
                    ts, mode, backend, whisper_model, language,
                    transcript, prompt, enhanced, enhancer_failed, audio_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    float(clock()),
                    mode,
                    backend,
                    whisper_model,
                    language,
                    transcript,
                    prompt,
                    1 if enhanced else 0,
                    1 if enhancer_failed else 0,
                    str(audio_path) if audio_path is not None else None,
                ),
            )
            row_id = int(cursor.lastrowid or 0)
            if limit > 0:
                conn.execute(
                    """
                    DELETE FROM transcripts WHERE id IN (
                        SELECT id FROM transcripts
                        ORDER BY ts DESC LIMIT -1 OFFSET ?
                    )
                    """,
                    (limit,),
                )
            conn.commit()
    except sqlite3.Error as exc:
        raise DictateError(f"failed to write dictate history at {path}: {exc}") from exc
    return row_id


def read_history(
    *,
    limit: int = 20,
    state_dir: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Return the most recent ``limit`` transcripts as plain dicts.

    Empty list if the history file does not exist yet. Order is newest-first.
    """

    path = history_path(state_dir)
    if not path.exists():
        return []
    try:
        with sqlite3.connect(str(path)) as conn:
            _ensure_history_schema(conn)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, ts, mode, backend, whisper_model, language,
                       transcript, prompt, enhanced, enhancer_failed, audio_path
                FROM transcripts
                ORDER BY ts DESC
                LIMIT ?
                """,
                (max(0, limit),),
            ).fetchall()
    except sqlite3.Error as exc:
        raise DictateError(f"failed to read dictate history at {path}: {exc}") from exc
    return [dict(row) for row in rows]


def clear_history(state_dir: Optional[Path] = None) -> None:
    path = history_path(state_dir)
    if path.exists():
        path.unlink()


# ----------------------------------------------------------------------------- 
# Top-level pipelines                                                           
# -----------------------------------------------------------------------------


@dataclass
class DictateResult:
    transcript: str
    prompt: str
    mode: str
    audio_path: Path
    enhanced: bool


def run_pipeline(
    audio_path: Path,
    *,
    mode: str = DEFAULT_MODE,
    enhance: bool = True,
    llm_config: Optional[LLMConfig] = None,
    transcriber: Optional[Callable[[Path, str, Optional[str]], str]] = None,
    enhancer: Optional[Callable[[LLMConfig, List[Dict[str, str]]], str]] = None,
    language: Optional[str] = None,
) -> DictateResult:
    """Transcribe → (optionally enhance) → return the result.

    The caller is responsible for writing the prompt to the clipboard and
    deleting the audio file. Splitting the I/O from the pure pipeline keeps
    ``run_pipeline`` cheap to unit-test.
    """

    if mode not in SUPPORTED_MODES:
        raise DictateError(
            f"unknown mode '{mode}'; expected one of {', '.join(SUPPORTED_MODES)}"
        )

    transcript = transcribe(audio_path, transcriber=transcriber, language=language)
    if not transcript:
        raise DictateError("transcription produced no text; nothing to enhance")

    do_enhance = enhance and not is_raw_mode(mode)
    if not do_enhance:
        return DictateResult(
            transcript=transcript,
            prompt=transcript,
            mode=mode,
            audio_path=audio_path,
            enhanced=False,
        )

    from . import pet_transient as _pt
    with _pt.pet_state("thinking", ttl_seconds=60.0):
        prompt = enhance_prompt(
            transcript,
            mode=mode,
            config=llm_config,
            caller=enhancer,
        )
    return DictateResult(
        transcript=transcript,
        prompt=prompt or transcript,
        mode=mode,
        audio_path=audio_path,
        enhanced=bool(prompt),
    )


def summarize_state(state: Optional[DictateState]) -> Dict[str, Any]:
    if state is None:
        return {"recording": False}
    alive = is_pid_alive(state.pid)
    payload = state.to_dict()
    payload["recording"] = alive
    payload["elapsed_seconds"] = max(0.0, time.time() - state.started_at)
    return payload


def render_argv(argv: List[str]) -> str:
    """Pretty-print an argv for human-readable logs (shell-escaped)."""

    return " ".join(shlex.quote(a) for a in argv)
