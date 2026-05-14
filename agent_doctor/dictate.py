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
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# ----------------------------------------------------------------------------- 
# Constants / configuration                                                     
# -----------------------------------------------------------------------------

DEFAULT_LLM_URL = "http://localhost:8080/v1/chat/completions"
DEFAULT_LLM_MODEL = "ds4"
DEFAULT_WHISPER_MODEL = "small"
DEFAULT_MODE = "chat"
SUPPORTED_MODES = ("chat", "coding", "research", "raw")

ENV_LLM_URL = "AGENT_DOCTOR_DICTATE_LLM_URL"
ENV_LLM_MODEL = "AGENT_DOCTOR_DICTATE_LLM_MODEL"
ENV_LLM_KEY = "AGENT_DOCTOR_DICTATE_LLM_KEY"
ENV_WHISPER_MODEL = "AGENT_DOCTOR_DICTATE_WHISPER_MODEL"
ENV_STATE_DIR = "AGENT_DOCTOR_DICTATE_STATE_DIR"
ENV_RECORDER = "AGENT_DOCTOR_DICTATE_RECORDER"  # "sox" | "ffmpeg"

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

_MODE_PROMPTS: Dict[str, str] = {
    "chat": (
        _BASE_RULES
        + "\nStyle: a clear, concise prompt suitable for a general chat "
        "assistant. Use the same language the user spoke in. Keep it short — "
        "do not pad."
    ),
    "coding": (
        _BASE_RULES
        + "\nStyle: a precise engineering task for a coding agent "
        "(Cursor, Claude Code, Codex). Structure as:\n"
        "  1. One-line task summary.\n"
        "  2. Concrete acceptance criteria (bulleted).\n"
        "  3. Constraints or non-goals the user mentioned.\n"
        "Only include sections the user actually specified; do not invent "
        "acceptance criteria the user did not state."
    ),
    "research": (
        _BASE_RULES
        + "\nStyle: a research brief suitable for a deep-research agent.\n"
        "  1. Question or objective in one sentence.\n"
        "  2. Entities/scope/timeframe the user mentioned.\n"
        "  3. Output format the user asked for, if any.\n"
        "Do not add sub-questions the user did not raise."
    ),
}


def mode_system_prompt(mode: str) -> str:
    """Return the system prompt for ``mode``.

    ``raw`` has no enhancement and therefore no system prompt; callers should
    check :func:`is_raw_mode` before invoking the enhancer.
    """

    if mode == "raw":
        raise DictateError("mode 'raw' bypasses the LLM enhancer")
    try:
        return _MODE_PROMPTS[mode]
    except KeyError as exc:
        raise DictateError(
            f"unknown dictate mode '{mode}'; expected one of "
            f"{', '.join(SUPPORTED_MODES)}"
        ) from exc


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
    proc = _spawn(argv)

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


def transcribe(
    audio_path: Path,
    *,
    model_name: Optional[str] = None,
    language: Optional[str] = None,
    transcriber: Optional[Callable[[Path, str, Optional[str]], str]] = None,
) -> str:
    """Transcribe ``audio_path`` using faster-whisper.

    ``transcriber`` is overridable for tests; in production we lazy-import
    ``faster_whisper`` and run it with sensible defaults.
    """

    chosen_model = model_name or os.environ.get(ENV_WHISPER_MODEL) or DEFAULT_WHISPER_MODEL
    fn = transcriber or _default_transcribe
    text = fn(audio_path, chosen_model, language)
    return text.strip()


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

    # ``int8`` is the best CPU/Apple-Silicon trade-off; ``compute_type='default'``
    # lets the library pick CoreML / Metal when available.
    model = WhisperModel(model_name, compute_type="default")
    segments, _info = model.transcribe(
        str(audio_path),
        language=language,
        vad_filter=True,
    )
    return "".join(segment.text for segment in segments)


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
    return LLMConfig(
        url=url or os.environ.get(ENV_LLM_URL) or DEFAULT_LLM_URL,
        model=model or os.environ.get(ENV_LLM_MODEL) or DEFAULT_LLM_MODEL,
        api_key=api_key or os.environ.get(ENV_LLM_KEY),
    )


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
    safe_title = title.replace('"', '\\"')
    safe_message = message.replace('"', '\\"')
    script = f'display notification "{safe_message}" with title "{safe_title}"'
    fn(["osascript", "-e", script])


def _default_osascript(argv: List[str]) -> int:
    proc = subprocess.run(argv, check=False)
    return proc.returncode


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
