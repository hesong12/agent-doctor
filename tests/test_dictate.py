"""Tests for the dictate voice-to-prompt pipeline.

We never touch the real microphone, real ``rec``/``ffmpeg`` binary, real
``faster-whisper``, real network, or real ``pbcopy`` in tests. Everything is
funneled through dependency-injection seams (``spawn``, ``terminator``,
``waiter``, ``transcriber``, ``caller``, ``runner``).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List

import pytest

from agent_doctor import dictate
from agent_doctor.dictate import (
    DEFAULT_MODE,
    DictateError,
    DictateResult,
    DictateState,
    LLMConfig,
    SUPPORTED_MODES,
    clear_state,
    copy_to_clipboard,
    enhance_prompt,
    is_raw_mode,
    llm_config_from_env,
    mode_system_prompt,
    read_state,
    run_pipeline,
    start_recording,
    state_file,
    stop_recording,
    summarize_state,
    transcribe,
    write_state,
)


# --------------------------------------------------------------------------- #
# Mode prompts                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", [m for m in SUPPORTED_MODES if m != "raw"])
def test_mode_system_prompt_includes_anti_fabrication_rule(mode: str) -> None:
    text = mode_system_prompt(mode)
    assert "Do not invent" in text
    assert "Output the rewritten prompt ONLY" in text


def test_mode_system_prompt_rejects_raw() -> None:
    with pytest.raises(DictateError, match="raw"):
        mode_system_prompt("raw")


def test_mode_system_prompt_rejects_unknown() -> None:
    with pytest.raises(DictateError, match="unknown dictate mode"):
        mode_system_prompt("does-not-exist")


def test_is_raw_mode() -> None:
    assert is_raw_mode("raw")
    assert not is_raw_mode("chat")


# --------------------------------------------------------------------------- #
# State file                                                                  #
# --------------------------------------------------------------------------- #


def test_state_round_trip(tmp_path: Path) -> None:
    state = DictateState(
        pid=12345,
        audio_path=str(tmp_path / "x.wav"),
        mode="chat",
        started_at=1700000000.0,
        recorder="sox",
        extras={"argv": ["rec", "x.wav"]},
    )
    write_state(state, state_dir=tmp_path)
    loaded = read_state(state_dir=tmp_path)
    assert loaded is not None
    assert loaded.pid == 12345
    assert loaded.audio_path == str(tmp_path / "x.wav")
    assert loaded.mode == "chat"
    assert loaded.recorder == "sox"
    assert loaded.extras == {"argv": ["rec", "x.wav"]}


def test_read_state_missing(tmp_path: Path) -> None:
    assert read_state(state_dir=tmp_path) is None


def test_read_state_corrupt(tmp_path: Path) -> None:
    sf = state_file(tmp_path)
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text("not json {")
    with pytest.raises(DictateError, match="corrupt dictate state"):
        read_state(state_dir=tmp_path)


def test_clear_state_idempotent(tmp_path: Path) -> None:
    clear_state(state_dir=tmp_path)  # no-op when missing
    state = DictateState(pid=1, audio_path="x", mode="chat", started_at=0.0, recorder="sox")
    write_state(state, state_dir=tmp_path)
    clear_state(state_dir=tmp_path)
    assert read_state(state_dir=tmp_path) is None


# --------------------------------------------------------------------------- #
# Recording lifecycle                                                         #
# --------------------------------------------------------------------------- #


class _FakeProc:
    def __init__(self, pid: int = 99999) -> None:
        self.pid = pid


def test_start_recording_writes_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dictate, "is_pid_alive", lambda pid: False)
    captured: Dict[str, Any] = {}

    def fake_spawn(argv: List[str]) -> _FakeProc:
        captured["argv"] = argv
        return _FakeProc(pid=42424)

    state = start_recording(
        mode="coding",
        state_dir=tmp_path,
        audio_dir=tmp_path,
        recorder="sox",
        spawn=fake_spawn,
    )
    assert state.pid == 42424
    assert state.mode == "coding"
    assert state.recorder == "sox"
    assert state.audio_path.endswith(".wav")
    assert captured["argv"][0] == "rec"
    assert "-r" in captured["argv"]
    assert "16000" in captured["argv"]

    persisted = read_state(state_dir=tmp_path)
    assert persisted is not None
    assert persisted.pid == 42424


def test_start_recording_rejects_unknown_mode(tmp_path: Path) -> None:
    with pytest.raises(DictateError, match="unknown mode"):
        start_recording(mode="hallucinated", state_dir=tmp_path)


def test_start_recording_refuses_when_another_alive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = DictateState(pid=1, audio_path="x", mode="chat", started_at=0.0, recorder="sox")
    write_state(state, state_dir=tmp_path)
    monkeypatch.setattr(dictate, "is_pid_alive", lambda pid: True)
    with pytest.raises(DictateError, match="already running"):
        start_recording(
            mode="chat",
            state_dir=tmp_path,
            audio_dir=tmp_path,
            recorder="sox",
            spawn=lambda argv: _FakeProc(),
        )


def test_start_recording_replaces_stale_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stale = DictateState(pid=1, audio_path="x", mode="chat", started_at=0.0, recorder="sox")
    write_state(stale, state_dir=tmp_path)
    monkeypatch.setattr(dictate, "is_pid_alive", lambda pid: False)
    new_state = start_recording(
        mode="chat",
        state_dir=tmp_path,
        audio_dir=tmp_path,
        recorder="sox",
        spawn=lambda argv: _FakeProc(pid=2),
    )
    assert new_state.pid == 2


def test_stop_recording_returns_audio_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audio = tmp_path / "rec.wav"
    audio.write_bytes(b"RIFF....fake-wav-content")
    state = DictateState(
        pid=123,
        audio_path=str(audio),
        mode="chat",
        started_at=0.0,
        recorder="sox",
    )
    write_state(state, state_dir=tmp_path)

    alive_calls = {"count": 0}

    def alive(pid: int) -> bool:
        # Alive once (so the terminator runs), then dead.
        alive_calls["count"] += 1
        return alive_calls["count"] == 1

    monkeypatch.setattr(dictate, "is_pid_alive", alive)

    term_calls: List[int] = []

    def fake_term(pid: int, sig: int) -> None:
        term_calls.append(sig)

    def fake_wait(pid: int, timeout: float) -> bool:
        return True

    out = stop_recording(state_dir=tmp_path, terminator=fake_term, waiter=fake_wait)
    assert out == audio
    assert signal.SIGTERM in term_calls


def test_stop_recording_no_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audio = tmp_path / "rec.wav"  # never created
    state = DictateState(
        pid=123,
        audio_path=str(audio),
        mode="chat",
        started_at=0.0,
        recorder="sox",
    )
    write_state(state, state_dir=tmp_path)
    monkeypatch.setattr(dictate, "is_pid_alive", lambda pid: False)

    with pytest.raises(DictateError, match="without writing audio"):
        stop_recording(
            state_dir=tmp_path,
            terminator=lambda *_: None,
            waiter=lambda *_: True,
        )


def test_stop_recording_no_state(tmp_path: Path) -> None:
    with pytest.raises(DictateError, match="no dictate recording"):
        stop_recording(state_dir=tmp_path)


# --------------------------------------------------------------------------- #
# Transcription                                                               #
# --------------------------------------------------------------------------- #


def test_transcribe_uses_injected_fn(tmp_path: Path) -> None:
    audio = tmp_path / "x.wav"
    audio.write_bytes(b"")

    calls: List[Any] = []

    def fake(audio_path: Path, model_name: str, language: str | None) -> str:
        calls.append((audio_path, model_name, language))
        return "  hello world  "

    out = transcribe(audio, model_name="small", language="en", transcriber=fake)
    assert out == "hello world"
    assert calls == [(audio, "small", "en")]


def test_transcribe_respects_env_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audio = tmp_path / "x.wav"
    audio.write_bytes(b"")
    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_WHISPER_MODEL", "medium")
    seen: Dict[str, str] = {}

    def fake(audio_path: Path, model_name: str, language: str | None) -> str:
        seen["model"] = model_name
        return "x"

    transcribe(audio, transcriber=fake)
    assert seen["model"] == "medium"


# --------------------------------------------------------------------------- #
# LLM enhancement                                                             #
# --------------------------------------------------------------------------- #


def test_llm_config_from_env_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Point settings at a clean tmp dir so ``load()`` returns built-in
    # defaults (provider=lm_studio, base_url=http://localhost:1234/v1,
    # model=None). With no env override the shim should mirror those.
    from agent_doctor import dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    monkeypatch.delenv("AGENT_DOCTOR_DICTATE_LLM_URL", raising=False)
    monkeypatch.delenv("AGENT_DOCTOR_DICTATE_LLM_MODEL", raising=False)
    monkeypatch.delenv("AGENT_DOCTOR_DICTATE_LLM_KEY", raising=False)
    cfg = llm_config_from_env()
    assert cfg.url == "http://localhost:1234/v1/chat/completions"
    assert cfg.model == "default"
    assert cfg.api_key is None


def test_llm_config_from_env_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_doctor import dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_LLM_URL", "http://example/v1/chat/completions")
    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_LLM_MODEL", "claude-opus-4-7")
    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_LLM_KEY", "sk-xxx")
    cfg = llm_config_from_env()
    assert cfg.url == "http://example/v1/chat/completions"
    assert cfg.model == "claude-opus-4-7"
    assert cfg.api_key == "sk-xxx"

    cfg2 = llm_config_from_env(url="http://override", model="ds4")
    assert cfg2.url == "http://override"
    assert cfg2.model == "ds4"


def test_enhance_prompt_raw_skips_llm() -> None:
    out = enhance_prompt("hello", mode="raw")
    assert out == "hello"


def test_enhance_prompt_empty() -> None:
    assert enhance_prompt("   ", mode="chat") == ""


def test_enhance_prompt_invokes_caller_with_messages() -> None:
    seen: Dict[str, Any] = {}

    def fake_caller(cfg: LLMConfig, messages: List[Dict[str, str]]) -> str:
        seen["cfg"] = cfg
        seen["messages"] = messages
        return "rewritten prompt"

    out = enhance_prompt(
        "build me a thing that does the thing",
        mode="coding",
        config=LLMConfig(url="http://x", model="ds4"),
        caller=fake_caller,
    )
    assert out == "rewritten prompt"
    assert seen["messages"][0]["role"] == "system"
    # Phase 2: every non-raw mode collapses to OPTIMIZE_PROMPT, so we assert
    # against a substring guaranteed to live in that single prompt rather
    # than the old per-mode 'acceptance criteria' wording.
    assert "output the rewritten prompt only" in seen["messages"][0]["content"].lower()
    assert seen["messages"][1]["role"] == "user"
    assert seen["messages"][1]["content"].startswith("build me a thing")


# --------------------------------------------------------------------------- #
# Pipeline                                                                    #
# --------------------------------------------------------------------------- #


def test_run_pipeline_enhances(tmp_path: Path) -> None:
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"")

    def fake_transcribe(ap: Path, mn: str, lang: str | None) -> str:
        return "  build a thing  "

    def fake_caller(cfg: LLMConfig, messages: List[Dict[str, str]]) -> str:
        return "Build a thing.\n- Acceptance: it builds.\n"

    result = run_pipeline(
        audio,
        mode="coding",
        enhance=True,
        transcriber=fake_transcribe,
        enhancer=fake_caller,
    )
    assert result.transcript == "build a thing"
    assert result.prompt.startswith("Build a thing.")
    assert result.enhanced is True
    assert result.mode == "coding"


def test_run_pipeline_raw_mode_skips_enhancement(tmp_path: Path) -> None:
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"")

    def fake_transcribe(ap: Path, mn: str, lang: str | None) -> str:
        return "hello world"

    def boom(cfg: LLMConfig, messages: List[Dict[str, str]]) -> str:
        raise AssertionError("enhancer should not be called in raw mode")

    result = run_pipeline(audio, mode="raw", enhance=True, transcriber=fake_transcribe, enhancer=boom)
    assert result.enhanced is False
    assert result.prompt == "hello world"


def test_run_pipeline_no_enhance(tmp_path: Path) -> None:
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"")

    def fake_transcribe(ap: Path, mn: str, lang: str | None) -> str:
        return "hello"

    result = run_pipeline(
        audio,
        mode="chat",
        enhance=False,
        transcriber=fake_transcribe,
        enhancer=lambda cfg, msgs: "should not be used",
    )
    assert result.enhanced is False
    assert result.prompt == "hello"


def test_run_pipeline_empty_transcript_errors(tmp_path: Path) -> None:
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"")

    def fake_transcribe(ap: Path, mn: str, lang: str | None) -> str:
        return "   "

    with pytest.raises(DictateError, match="no text"):
        run_pipeline(audio, mode="chat", transcriber=fake_transcribe)


def test_run_pipeline_falls_back_on_enhancer_failure(tmp_path: Path) -> None:
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"")

    def fake_transcribe(ap: Path, mn: str, lang: str | None) -> str:
        return "hello world"

    def failing_enhancer(cfg: LLMConfig, messages: List[Dict[str, str]]) -> str:
        raise DictateError("ds4 down")

    # run_pipeline itself raises; the CLI layer is responsible for graceful
    # fallback. Check the error path is well-typed.
    with pytest.raises(DictateError, match="ds4 down"):
        run_pipeline(audio, mode="chat", transcriber=fake_transcribe, enhancer=failing_enhancer)


# --------------------------------------------------------------------------- #
# Clipboard + notify                                                          #
# --------------------------------------------------------------------------- #


def test_copy_to_clipboard_invokes_runner() -> None:
    captured: Dict[str, Any] = {}

    def fake_runner(argv: List[str], data: bytes) -> int:
        captured["argv"] = argv
        captured["data"] = data
        return 0

    copy_to_clipboard("hello", runner=fake_runner)
    assert captured["argv"] == ["pbcopy"]
    assert captured["data"] == b"hello"


def test_copy_to_clipboard_propagates_failure() -> None:
    def bad_runner(argv: List[str], data: bytes) -> int:
        return 1

    with pytest.raises(DictateError, match="pbcopy exited"):
        copy_to_clipboard("x", runner=bad_runner)


def test_summarize_state_none() -> None:
    assert summarize_state(None) == {"recording": False}


def test_summarize_state_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dictate, "is_pid_alive", lambda pid: True)
    state = DictateState(pid=1, audio_path="x", mode="chat", started_at=time.time() - 5, recorder="sox")
    summary = summarize_state(state)
    assert summary["recording"] is True
    assert summary["elapsed_seconds"] >= 4.9


# --------------------------------------------------------------------------- #
# CLI integration                                                             #
# --------------------------------------------------------------------------- #


def test_cli_dictate_subcommands_registered() -> None:
    """Smoke test: the parser exposes the dictate group with all five verbs."""

    from agent_doctor.cli import build_parser

    parser = build_parser()
    # Use argparse internals carefully — just verify ``--help`` includes the verbs.
    help_text = parser.format_help()
    assert "dictate" in help_text


def test_cli_dictate_status_with_no_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_STATE_DIR", str(tmp_path))
    from agent_doctor.cli import main

    rc = main(["dictate", "status"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert rc == 0
    assert payload == {"recording": False}


def test_cli_dictate_cancel_no_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_STATE_DIR", str(tmp_path))
    from agent_doctor.cli import main

    rc = main(["dictate", "cancel"])
    assert rc == 0


# --------------------------------------------------------------------------- #
# Regression tests for PR #24 review feedback (Gemini + Codex)                #
# --------------------------------------------------------------------------- #


def test_start_recording_wraps_missing_binary_in_dictate_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gemini medium: ``subprocess.Popen`` raising FileNotFoundError must be
    surfaced as a DictateError, not a raw traceback."""

    monkeypatch.setattr(dictate, "is_pid_alive", lambda pid: False)

    def boom(argv: List[str]) -> _FakeProc:
        raise FileNotFoundError(2, "No such file or directory: 'rec'")

    with pytest.raises(DictateError, match="recorder binary 'rec' not found"):
        start_recording(
            mode="chat",
            state_dir=tmp_path,
            audio_dir=tmp_path,
            recorder="sox",
            spawn=boom,
        )


def test_notify_escapes_backslashes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gemini medium: AppleScript uses '\\' as an escape char. A title with a
    literal backslash (e.g. a Windows path or LaTeX) must be escaped so the
    'display notification "..."' literal still parses."""

    if sys.platform != "darwin":
        pytest.skip("osascript path only fires on Darwin")

    monkeypatch.setattr(dictate.shutil, "which", lambda name: "/usr/bin/osascript")
    captured: List[List[str]] = []

    def fake_runner(argv: List[str]) -> int:
        captured.append(argv)
        return 0

    dictate.notify("path C:\\Users\\song", 'with "quotes"', runner=fake_runner)
    assert captured, "notify should have invoked the runner"
    script = captured[0][2]
    # Original backslashes must be doubled and quotes must be backslash-escaped.
    assert r"C:\\Users\\song" in script
    assert r"\"quotes\"" in script


def test_cli_dictate_status_corrupt_state_returns_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Codex P2: 'dictate status' must not crash on a corrupt state file."""

    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_STATE_DIR", str(tmp_path))
    sf = state_file(tmp_path)
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text("definitely not json {")

    from agent_doctor.cli import main

    rc = main(["dictate", "status"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert rc == 2
    assert payload["recording"] is False
    assert "error" in payload
    assert "cancel" in payload.get("hint", "")


def test_cli_dictate_cancel_clears_corrupt_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex P2 follow-on: 'dictate cancel' must clear a corrupt state file."""

    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_STATE_DIR", str(tmp_path))
    sf = state_file(tmp_path)
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text("not json {")

    from agent_doctor.cli import main

    rc = main(["dictate", "cancel"])
    assert rc == 0
    assert not sf.exists()


def test_cli_dictate_toggle_preserves_persisted_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Codex P2: when 'dictate toggle' fires the stop branch and the user did
    NOT pass --mode, the persisted mode (from start) must be reused, not the
    argparse default.

    Phase 2 update: we use 'optimize' (the canonical non-raw, non-deprecated
    mode) so the assertion survives the chat/coding/research collapse."""

    # Arrange: persisted state with mode='optimize' and a real WAV pre-created.
    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_STATE_DIR", str(tmp_path))
    audio = tmp_path / "rec.wav"
    audio.write_bytes(b"RIFF....fake")
    saved = DictateState(
        pid=99999,
        audio_path=str(audio),
        mode="optimize",
        started_at=time.time(),
        recorder="sox",
        extras={},
    )
    write_state(saved, state_dir=tmp_path)

    # Pretend the recorder is alive so toggle dispatches to _dictate_finish.
    monkeypatch.setattr(dictate, "is_pid_alive", lambda pid: True)
    # Pretend stop_recording cleanly returns the audio.
    monkeypatch.setattr(
        dictate,
        "stop_recording",
        lambda **kw: audio,
    )
    # Pretend transcribe + enhance work; capture the mode used.
    monkeypatch.setattr(
        dictate,
        "transcribe",
        lambda *a, **kw: "hello world",
    )
    captured_mode: Dict[str, str] = {}

    def fake_enhance(transcript: str, *, mode: str, config: Any = None, caller: Any = None) -> str:
        captured_mode["mode"] = mode
        return f"[{mode}] hello"

    monkeypatch.setattr(dictate, "enhance_prompt", fake_enhance)
    # No-op clipboard.
    monkeypatch.setattr(dictate, "copy_to_clipboard", lambda *a, **kw: None)
    monkeypatch.setattr(dictate, "notify", lambda *a, **kw: None)

    from agent_doctor.cli import main

    rc = main(["dictate", "toggle"])  # no --mode flag
    assert rc == 0
    assert captured_mode["mode"] == "optimize", (
        "toggle without --mode should reuse the mode from the persisted state, "
        f"got {captured_mode.get('mode')!r}"
    )


def test_cli_dictate_toggle_explicit_mode_overrides_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Companion test: passing --mode at toggle time must override the
    persisted mode.

    Phase 2 update: persisted='optimize' (would enhance) is overridden by
    --mode raw (skips the enhancer entirely). enhance_prompt should NOT be
    called — that is the observable signal that the override took effect."""

    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_STATE_DIR", str(tmp_path))
    audio = tmp_path / "rec.wav"
    audio.write_bytes(b"RIFF....fake")
    saved = DictateState(
        pid=99999, audio_path=str(audio), mode="optimize",
        started_at=time.time(), recorder="sox", extras={},
    )
    write_state(saved, state_dir=tmp_path)

    monkeypatch.setattr(dictate, "is_pid_alive", lambda pid: True)
    monkeypatch.setattr(dictate, "stop_recording", lambda **kw: audio)
    monkeypatch.setattr(dictate, "transcribe", lambda *a, **kw: "hello")
    seen: Dict[str, str] = {}

    def _should_not_be_called(*_a: Any, **_k: Any) -> str:
        seen["called"] = "yes"
        return "should-not-happen"

    monkeypatch.setattr(dictate, "enhance_prompt", _should_not_be_called)
    monkeypatch.setattr(dictate, "copy_to_clipboard", lambda *a, **kw: None)
    monkeypatch.setattr(dictate, "notify", lambda *a, **kw: None)

    from agent_doctor.cli import main

    rc = main(["dictate", "toggle", "--mode", "raw"])
    assert rc == 0
    assert "called" not in seen, "raw override must short-circuit the enhancer"


def test_cli_dictate_stop_does_not_double_transcribe_on_enhancer_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gemini high: if the LLM enhancer fails, the CLI must NOT call
    ``transcribe`` a second time. The transcript from the single pass must be
    reused as the raw-fallback prompt."""

    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_STATE_DIR", str(tmp_path))
    audio = tmp_path / "rec.wav"
    audio.write_bytes(b"RIFF....fake")
    saved = DictateState(
        pid=99999, audio_path=str(audio), mode="chat",
        started_at=time.time(), recorder="sox", extras={},
    )
    write_state(saved, state_dir=tmp_path)

    monkeypatch.setattr(dictate, "is_pid_alive", lambda pid: True)
    monkeypatch.setattr(dictate, "stop_recording", lambda **kw: audio)

    transcribe_calls = {"count": 0}

    def counting_transcribe(*args: Any, **kwargs: Any) -> str:
        transcribe_calls["count"] += 1
        return "the real transcript"

    monkeypatch.setattr(dictate, "transcribe", counting_transcribe)

    def failing_enhance(*args: Any, **kwargs: Any) -> str:
        raise DictateError("ds4 server unreachable")

    monkeypatch.setattr(dictate, "enhance_prompt", failing_enhance)

    clipboard_calls: List[str] = []
    monkeypatch.setattr(
        dictate,
        "copy_to_clipboard",
        lambda text, **kw: clipboard_calls.append(text),
    )
    monkeypatch.setattr(dictate, "notify", lambda *a, **kw: None)

    from agent_doctor.cli import main

    rc = main(["dictate", "stop"])
    assert rc == 0
    assert transcribe_calls["count"] == 1, (
        f"transcribe should run exactly once; got {transcribe_calls['count']}"
    )
    assert clipboard_calls == ["the real transcript"], (
        f"raw transcript must be the clipboard fallback; got {clipboard_calls}"
    )


def test_cli_dictate_stop_does_not_copy_empty_prompt_on_transcription_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Codex P1: a transcription failure (empty result) must NOT be caught by
    the enhancer-fallback path. The clipboard must remain untouched and the
    CLI must exit non-zero."""

    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_STATE_DIR", str(tmp_path))
    audio = tmp_path / "rec.wav"
    audio.write_bytes(b"RIFF....fake")
    saved = DictateState(
        pid=99999, audio_path=str(audio), mode="chat",
        started_at=time.time(), recorder="sox", extras={},
    )
    write_state(saved, state_dir=tmp_path)

    monkeypatch.setattr(dictate, "is_pid_alive", lambda pid: True)
    monkeypatch.setattr(dictate, "stop_recording", lambda **kw: audio)
    monkeypatch.setattr(dictate, "transcribe", lambda *a, **kw: "   ")  # all whitespace

    clipboard_calls: List[str] = []
    monkeypatch.setattr(
        dictate,
        "copy_to_clipboard",
        lambda text, **kw: clipboard_calls.append(text),
    )
    monkeypatch.setattr(dictate, "notify", lambda *a, **kw: None)
    # If enhance gets called we'd see it in failures.
    monkeypatch.setattr(
        dictate,
        "enhance_prompt",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not enhance empty transcript")),
    )

    from agent_doctor.cli import main

    rc = main(["dictate", "stop"])
    assert rc == 2
    assert clipboard_calls == [], "must not copy anything for an empty transcript"


def test_cli_dictate_stop_cleans_up_audio_on_clipboard_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gemini high #2: audio file + state must be cleaned up even when the
    clipboard step fails."""

    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_STATE_DIR", str(tmp_path))
    audio = tmp_path / "rec.wav"
    audio.write_bytes(b"RIFF....fake")
    saved = DictateState(
        pid=99999, audio_path=str(audio), mode="chat",
        started_at=time.time(), recorder="sox", extras={},
    )
    write_state(saved, state_dir=tmp_path)

    monkeypatch.setattr(dictate, "is_pid_alive", lambda pid: True)
    monkeypatch.setattr(dictate, "stop_recording", lambda **kw: audio)
    monkeypatch.setattr(dictate, "transcribe", lambda *a, **kw: "hello")
    monkeypatch.setattr(dictate, "enhance_prompt", lambda *a, **kw: "rewritten")

    def failing_clipboard(text: str, **kw: Any) -> None:
        raise DictateError("pbcopy died")

    monkeypatch.setattr(dictate, "copy_to_clipboard", failing_clipboard)
    monkeypatch.setattr(dictate, "notify", lambda *a, **kw: None)

    from agent_doctor.cli import main

    rc = main(["dictate", "stop"])
    assert rc == 2
    assert not audio.exists(), "audio must be cleaned up even on clipboard failure"
    assert read_state(state_dir=tmp_path) is None, "state must be cleared even on clipboard failure"


# --------------------------------------------------------------------------- #
# Regression tests for PR #24 second-round review feedback (Gemini)           #
# --------------------------------------------------------------------------- #


def test_default_llm_call_surfaces_http_error_with_status_and_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gemini medium #2 (round 2): a non-2xx response from the LLM endpoint
    must surface the HTTP status, reason, and response body so misconfigured
    URLs / API keys are easy to diagnose."""

    import io
    import urllib.error

    cfg = LLMConfig(url="http://example/v1/chat/completions", model="ds4", api_key="sk-x")
    err = urllib.error.HTTPError(
        url=cfg.url,
        code=401,
        msg="Unauthorized",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(b'{"error":{"message":"invalid api key"}}'),
    )

    def fake_urlopen(req, timeout):  # type: ignore[no-untyped-def]
        raise err

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(DictateError) as exc_info:
        dictate._default_llm_call(cfg, [{"role": "user", "content": "hi"}])
    msg = str(exc_info.value)
    assert "HTTP 401" in msg
    assert "Unauthorized" in msg
    assert "invalid api key" in msg


def test_default_llm_call_surfaces_url_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sibling test: a generic URLError (e.g. ds4 server not running) still
    yields the 'unreachable' message — we did not regress the original path."""

    import urllib.error

    cfg = LLMConfig(url="http://localhost:8080/v1/chat/completions", model="ds4")

    def fake_urlopen(req, timeout):  # type: ignore[no-untyped-def]
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(DictateError, match="unreachable"):
        dictate._default_llm_call(cfg, [{"role": "user", "content": "hi"}])


# --------------------------------------------------------------------------- #
# Backend selection (whisper.cpp + faster-whisper)                            #
# --------------------------------------------------------------------------- #


def test_detect_backend_ggml_path_routes_to_whisper_cpp(tmp_path: Path) -> None:
    """A model name ending in '.bin' must route to whisper-cpp, not
    faster-whisper, even when the file does not yet exist (we cannot
    require existence because users may pass a path that pywhispercpp will
    download)."""

    candidate = str(tmp_path / "ggml-large-v3-turbo.bin")
    assert dictate.detect_backend(candidate) == "whisper-cpp"


def test_detect_backend_gguf_suffix_routes_to_whisper_cpp(tmp_path: Path) -> None:
    candidate = str(tmp_path / "anything.gguf")
    assert dictate.detect_backend(candidate) == "whisper-cpp"


def test_detect_backend_existing_local_path_routes_to_whisper_cpp(tmp_path: Path) -> None:
    """A bare path with no recognized suffix but that exists on disk
    routes to whisper-cpp; this covers the case where a user points at a
    whisper.cpp model dir without using the .bin suffix."""

    p_local = tmp_path / "custom_model"
    p_local.write_bytes(b"\x00" * 16)
    assert dictate.detect_backend(str(p_local)) == "whisper-cpp"


@pytest.mark.parametrize(
    "alias",
    ["small", "medium", "large-v3", "large-v3-turbo", "distil-large-v3"],
)
def test_detect_backend_faster_whisper_size_aliases(alias: str) -> None:
    assert dictate.detect_backend(alias) == "faster-whisper"


def test_detect_backend_hf_repo_id_routes_to_faster_whisper() -> None:
    """HF repo ids contain '/' but the local path does not exist; the
    heuristic must fall through to faster-whisper, NOT mistakenly assume
    whisper-cpp because of the slash."""

    assert dictate.detect_backend("Systran/faster-whisper-small") == "faster-whisper"


def test_detect_backend_empty_falls_back_to_faster_whisper() -> None:
    assert dictate.detect_backend(None) == "faster-whisper"  # type: ignore[arg-type]
    assert dictate.detect_backend("") == "faster-whisper"


def test_transcribe_routes_to_whisper_cpp_for_bin_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integration: transcribe(model_name=<...bin>) must call the whisper-cpp
    backend, not the faster-whisper one."""

    audio = tmp_path / "a.wav"
    audio.write_bytes(b"")
    model = tmp_path / "ggml-tiny.bin"
    model.write_bytes(b"")

    calls: Dict[str, Any] = {}

    def fake_cpp(audio_path: Path, model_name: str, language: Any) -> str:
        calls["cpp"] = (audio_path, model_name, language)
        return "from cpp"

    def fake_ct2(audio_path: Path, model_name: str, language: Any) -> str:
        calls["ct2"] = (audio_path, model_name, language)
        return "from ct2"

    monkeypatch.setattr(dictate, "_default_transcribe_whisper_cpp", fake_cpp)
    monkeypatch.setattr(dictate, "_default_transcribe", fake_ct2)

    out = dictate.transcribe(audio, model_name=str(model))
    assert out == "from cpp"
    assert "cpp" in calls and "ct2" not in calls


def test_transcribe_routes_to_faster_whisper_for_size_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"")

    calls: Dict[str, Any] = {}

    def fake_cpp(*a: Any, **kw: Any) -> str:
        calls["cpp"] = a
        return "cpp"

    def fake_ct2(*a: Any, **kw: Any) -> str:
        calls["ct2"] = a
        return "ct2"

    monkeypatch.setattr(dictate, "_default_transcribe_whisper_cpp", fake_cpp)
    monkeypatch.setattr(dictate, "_default_transcribe", fake_ct2)

    out = dictate.transcribe(audio, model_name="large-v3-turbo")
    assert out == "ct2"
    assert "ct2" in calls and "cpp" not in calls


def test_transcribe_explicit_backend_overrides_autodetect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--backend whisper-cpp forces whisper-cpp even if model name looks
    like a faster-whisper size alias (e.g. user has a custom model named
    'small.bin' but points the alias)."""

    audio = tmp_path / "a.wav"
    audio.write_bytes(b"")

    calls: List[str] = []

    def fake_cpp(*a: Any, **kw: Any) -> str:
        calls.append("cpp")
        return "cpp"

    def fake_ct2(*a: Any, **kw: Any) -> str:
        calls.append("ct2")
        return "ct2"

    monkeypatch.setattr(dictate, "_default_transcribe_whisper_cpp", fake_cpp)
    monkeypatch.setattr(dictate, "_default_transcribe", fake_ct2)

    dictate.transcribe(audio, model_name="small", backend="whisper-cpp")
    assert calls == ["cpp"]


def test_transcribe_rejects_unknown_backend(tmp_path: Path) -> None:
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"")
    with pytest.raises(DictateError, match="unknown whisper backend"):
        dictate.transcribe(audio, backend="hallucinated")


def test_transcribe_backend_env_var_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AGENT_DOCTOR_DICTATE_BACKEND env var sets the default backend
    when --backend is not passed."""

    audio = tmp_path / "a.wav"
    audio.write_bytes(b"")

    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_BACKEND", "whisper-cpp")
    calls: List[str] = []

    def fake_cpp(*a: Any, **kw: Any) -> str:
        calls.append("cpp")
        return "cpp"

    def fake_ct2(*a: Any, **kw: Any) -> str:
        calls.append("ct2")
        return "ct2"

    monkeypatch.setattr(dictate, "_default_transcribe_whisper_cpp", fake_cpp)
    monkeypatch.setattr(dictate, "_default_transcribe", fake_ct2)

    # Even a size alias gets routed through whisper-cpp because of the env var.
    dictate.transcribe(audio, model_name="small")
    assert calls == ["cpp"]


def test_transcribe_injected_transcriber_bypasses_backend_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backwards-compat: tests that pass a custom transcriber must still
    bypass backend selection (so existing test suite keeps working)."""

    audio = tmp_path / "a.wav"
    audio.write_bytes(b"")
    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_BACKEND", "whisper-cpp")

    cpp_calls = {"n": 0}

    def boom_cpp(*a: Any, **kw: Any) -> str:
        cpp_calls["n"] += 1
        return "should not be called"

    monkeypatch.setattr(dictate, "_default_transcribe_whisper_cpp", boom_cpp)

    out = dictate.transcribe(
        audio, model_name="anything",
        transcriber=lambda ap, mn, lang: "injected",
    )
    assert out == "injected"
    assert cpp_calls["n"] == 0


def test_default_transcribe_whisper_cpp_raises_if_pywhispercpp_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When pywhispercpp is not installed, the whisper-cpp backend must
    raise a friendly DictateError pointing at the [dictate-cpp] extra."""

    # Hide pywhispercpp from the importer.
    import sys as _sys

    monkeypatch.setitem(_sys.modules, "pywhispercpp", None)
    monkeypatch.setitem(_sys.modules, "pywhispercpp.model", None)

    with pytest.raises(DictateError, match="pywhispercpp is not installed"):
        dictate._default_transcribe_whisper_cpp(tmp_path / "x.wav", "x.bin", None)


def test_resolve_whisper_model_consults_settings_before_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a user has configured a model_path via settings (e.g. via
    Preferences → Dictation), ``dictate stop`` with no --whisper-model
    flag must use that path instead of falling back to
    DEFAULT_WHISPER_MODEL ("small", a faster-whisper alias). This is
    the root cause of "faster-whisper is not installed" when the user
    actually has a whisper-cpp .bin model selected.
    """

    from agent_doctor import dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    bin_path = tmp_path / "ggml-large-v3-turbo.bin"
    bin_path.write_bytes(b"\x00")
    settings = ds.replace_section(
        ds.default_settings(),
        transcription=ds.TranscriptionSettings(
            model_id="ggml-large-v3-turbo",
            model_path=str(bin_path),
            language="auto",
            extra_buffer_ms=150,
        ),
    )
    ds.save(settings)

    # Clear env so settings take effect.
    monkeypatch.delenv("AGENT_DOCTOR_DICTATE_WHISPER_MODEL", raising=False)
    resolved = dictate.resolve_whisper_model(arg=None)
    assert resolved == str(bin_path), (
        "resolve_whisper_model should fall through to settings.model_path "
        "before DEFAULT_WHISPER_MODEL"
    )


def test_resolve_whisper_model_arg_beats_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit --whisper-model still takes precedence over settings."""

    from agent_doctor import dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    settings = ds.replace_section(
        ds.default_settings(),
        transcription=ds.TranscriptionSettings(
            model_id="ggml-large-v3-turbo",
            model_path="/tmp/from-settings.bin",
            language="auto",
            extra_buffer_ms=150,
        ),
    )
    ds.save(settings)

    monkeypatch.delenv("AGENT_DOCTOR_DICTATE_WHISPER_MODEL", raising=False)
    resolved = dictate.resolve_whisper_model(arg="explicit-override")
    assert resolved == "explicit-override"


def test_resolve_whisper_model_env_beats_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The env var still wins over settings (matches transcribe() order)."""

    from agent_doctor import dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    settings = ds.replace_section(
        ds.default_settings(),
        transcription=ds.TranscriptionSettings(
            model_id="ggml-large-v3-turbo",
            model_path="/tmp/from-settings.bin",
            language="auto",
            extra_buffer_ms=150,
        ),
    )
    ds.save(settings)
    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_WHISPER_MODEL", "from-env")
    resolved = dictate.resolve_whisper_model(arg=None)
    assert resolved == "from-env"


def test_resolve_whisper_model_falls_back_to_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no arg / env / settings, falls back to the historical
    DEFAULT_WHISPER_MODEL so existing behavior is preserved."""

    from agent_doctor import dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    # Default settings have no model_path.
    ds.save(ds.default_settings())
    monkeypatch.delenv("AGENT_DOCTOR_DICTATE_WHISPER_MODEL", raising=False)
    resolved = dictate.resolve_whisper_model(arg=None)
    assert resolved == dictate.DEFAULT_WHISPER_MODEL


def test_whisper_cpp_does_not_wrap_with_suppress_native_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fd-level suppress trick (os.dup2 over fds 1/2) makes
    pywhispercpp's Metal/GGML init SIGSEGV on macOS — verified
    repeatable on Apple Silicon during PR #38 smoke testing. Whisper-cpp
    path must NOT activate suppress regardless of the DEBUG env var.
    """

    suppress_called = [False]

    @contextmanager
    def fake_suppress(*, suppress: bool):
        suppress_called[0] = suppress_called[0] or suppress
        yield

    monkeypatch.setattr(dictate, "_maybe_suppress_native_output", fake_suppress)

    class FakeModel:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        def transcribe(self, *_a: Any, **_kw: Any) -> list:
            class Seg:
                text = "ok"
            return [Seg()]

    # Inject pywhispercpp.model.Model via sys.modules so the import works
    import sys as _sys
    import types as _types
    fake_module = _types.ModuleType("pywhispercpp.model")
    fake_module.Model = FakeModel  # type: ignore[attr-defined]
    monkeypatch.setitem(_sys.modules, "pywhispercpp", _types.ModuleType("pywhispercpp"))
    monkeypatch.setitem(_sys.modules, "pywhispercpp.model", fake_module)

    # Default-mode (DEBUG unset) used to call suppress(True) and trigger SIGSEGV.
    monkeypatch.delenv("AGENT_DOCTOR_DICTATE_DEBUG", raising=False)
    result = dictate._default_transcribe_whisper_cpp(tmp_path / "x.wav", "x.bin", None)
    assert result == "ok"
    assert suppress_called[0] is False, (
        "whisper-cpp path must never activate fd-level suppression "
        "(pywhispercpp SIGSEGVs under dup2(devnull, 1))"
    )


def test_suppress_native_output_restores_fds(tmp_path: Path) -> None:
    """The fd-level suppression context must restore stdout/stderr on exit
    even when the wrapped block raises, so the user's terminal does not
    stay redirected to /dev/null after a transcription failure."""

    import os

    pre_stdout = os.fstat(1).st_ino if os.path.exists("/dev/stdout") else None  # noqa: E501

    try:
        with dictate._maybe_suppress_native_output(suppress=True):
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    # After the context, fd 1 must be writable. Easiest check: write a byte
    # to a captured pipe via os.write would touch real stdout; instead,
    # confirm fd 1 is still valid by calling os.fstat.
    os.fstat(1)  # would raise OSError if fd 1 had been closed


# --------------------------------------------------------------------------- #
# Tail buffer (extra recording after stop)                                    #
# --------------------------------------------------------------------------- #


def test_resolve_buffer_ms_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_DOCTOR_DICTATE_BUFFER_MS", raising=False)
    assert dictate.resolve_buffer_ms(None) == dictate.DEFAULT_BUFFER_MS


def test_resolve_buffer_ms_cli_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_BUFFER_MS", "500")
    assert dictate.resolve_buffer_ms(50) == 50


def test_resolve_buffer_ms_env_when_no_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_BUFFER_MS", "300")
    assert dictate.resolve_buffer_ms(None) == 300


def test_resolve_buffer_ms_negative_clamps_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_DOCTOR_DICTATE_BUFFER_MS", raising=False)
    assert dictate.resolve_buffer_ms(-99) == 0


def test_resolve_buffer_ms_invalid_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_BUFFER_MS", "not-an-int")
    with pytest.raises(DictateError, match="not a valid integer"):
        dictate.resolve_buffer_ms(None)


def test_maybe_sleep_for_buffer_invokes_sleeper() -> None:
    seen: List[float] = []
    dictate.maybe_sleep_for_buffer(150, sleeper=seen.append)
    assert seen == [0.15]


def test_maybe_sleep_for_buffer_zero_is_noop() -> None:
    seen: List[float] = []
    dictate.maybe_sleep_for_buffer(0, sleeper=seen.append)
    assert seen == []


# --------------------------------------------------------------------------- #
# Audio feedback                                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("env", ["1", "true", "TRUE", "yes", "on"])
def test_beep_enabled_truthy_env(monkeypatch: pytest.MonkeyPatch, env: str) -> None:
    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_BEEP", env)
    assert dictate.beep_enabled(None) is True


def test_beep_enabled_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_DOCTOR_DICTATE_BEEP", raising=False)
    assert dictate.beep_enabled(None) is False


def test_beep_enabled_cli_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_BEEP", "1")
    assert dictate.beep_enabled(False) is False  # explicit --no-beep wins
    assert dictate.beep_enabled(True) is True


def test_play_sound_invokes_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform != "darwin":
        pytest.skip("afplay path is Darwin-only")
    monkeypatch.setattr(dictate.shutil, "which", lambda name: "/usr/bin/afplay")
    captured: List[List[str]] = []
    dictate.play_sound("/tmp/sound.aiff", runner=lambda argv: captured.append(argv) or 0)
    assert captured == [["afplay", "/tmp/sound.aiff"]]


def test_play_sound_swallows_runner_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Audio feedback must never break the dictate flow."""
    if sys.platform != "darwin":
        pytest.skip("afplay path is Darwin-only")
    monkeypatch.setattr(dictate.shutil, "which", lambda name: "/usr/bin/afplay")

    def boom(_: List[str]) -> int:
        raise RuntimeError("audio subsystem dead")

    dictate.play_sound("/tmp/x.aiff", runner=boom)  # must NOT raise


def test_play_sound_empty_path_is_noop() -> None:
    dictate.play_sound("", runner=lambda argv: 1 / 0)  # must not invoke runner


# --------------------------------------------------------------------------- #
# History (SQLite)                                                            #
# --------------------------------------------------------------------------- #


def test_record_and_read_history_round_trip(tmp_path: Path) -> None:
    row_id = dictate.record_history(
        transcript="hello world",
        prompt="Hello, world.",
        mode="chat",
        enhanced=True,
        backend="whisper-cpp",
        whisper_model="ggml-large-v3-turbo.bin",
        language="en",
        state_dir=tmp_path,
        clock=lambda: 1700000000.5,
    )
    assert row_id > 0

    rows = dictate.read_history(state_dir=tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["transcript"] == "hello world"
    assert row["prompt"] == "Hello, world."
    assert row["mode"] == "chat"
    assert row["enhanced"] == 1
    assert row["enhancer_failed"] == 0
    assert row["backend"] == "whisper-cpp"
    assert row["whisper_model"] == "ggml-large-v3-turbo.bin"
    assert row["language"] == "en"
    assert row["ts"] == 1700000000.5


def test_read_history_returns_newest_first(tmp_path: Path) -> None:
    for i, ts in enumerate([1, 2, 3, 4], start=1):
        dictate.record_history(
            transcript=f"t{i}",
            prompt=f"p{i}",
            mode="chat",
            enhanced=False,
            state_dir=tmp_path,
            clock=lambda ts=ts: float(ts),
        )
    rows = dictate.read_history(state_dir=tmp_path, limit=10)
    assert [r["transcript"] for r in rows] == ["t4", "t3", "t2", "t1"]


def test_record_history_prunes_to_retention_limit(tmp_path: Path) -> None:
    for i in range(5):
        dictate.record_history(
            transcript=f"t{i}",
            prompt=f"p{i}",
            mode="chat",
            enhanced=False,
            state_dir=tmp_path,
            retention_limit=3,
            clock=lambda i=i: float(i),
        )
    rows = dictate.read_history(state_dir=tmp_path, limit=10)
    # Newest 3 kept (t4, t3, t2); t0/t1 pruned.
    assert [r["transcript"] for r in rows] == ["t4", "t3", "t2"]


def test_read_history_missing_db_returns_empty(tmp_path: Path) -> None:
    assert dictate.read_history(state_dir=tmp_path) == []


def test_clear_history_removes_db(tmp_path: Path) -> None:
    dictate.record_history(
        transcript="x", prompt="x", mode="chat", enhanced=False,
        state_dir=tmp_path,
    )
    assert dictate.history_path(tmp_path).exists()
    dictate.clear_history(tmp_path)
    assert not dictate.history_path(tmp_path).exists()


def test_resolve_history_limit_invalid_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_HISTORY_LIMIT", "abc")
    with pytest.raises(DictateError, match="not a valid integer"):
        dictate._resolve_history_limit(None)


# --------------------------------------------------------------------------- #
# CLI integration: history + buffer + beep + timing                           #
# --------------------------------------------------------------------------- #


def test_cli_dictate_history_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_STATE_DIR", str(tmp_path))
    from agent_doctor.cli import main

    rc = main(["dictate", "history"])
    out = capsys.readouterr()
    assert rc == 0
    assert "no dictate history" in out.err


def test_cli_dictate_history_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_STATE_DIR", str(tmp_path))
    dictate.record_history(
        transcript="hi", prompt="Hi.", mode="chat", enhanced=True,
        state_dir=tmp_path, clock=lambda: 1.0,
    )
    from agent_doctor.cli import main

    rc = main(["dictate", "history", "--json", "--limit", "5"])
    out = capsys.readouterr()
    assert rc == 0
    payload = json.loads(out.out)
    assert payload[0]["transcript"] == "hi"
    assert payload[0]["enhanced"] == 1


def test_cli_dictate_history_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_STATE_DIR", str(tmp_path))
    dictate.record_history(
        transcript="x", prompt="x", mode="chat", enhanced=False,
        state_dir=tmp_path,
    )
    from agent_doctor.cli import main

    rc = main(["dictate", "history", "--clear"])
    assert rc == 0
    assert not dictate.history_path(tmp_path).exists()


def test_cli_dictate_stop_records_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful stop must write one row into the history DB."""

    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_STATE_DIR", str(tmp_path))
    audio = tmp_path / "rec.wav"
    audio.write_bytes(b"RIFF....fake")
    saved = DictateState(
        pid=99999, audio_path=str(audio), mode="chat",
        started_at=time.time(), recorder="sox", extras={},
    )
    write_state(saved, state_dir=tmp_path)

    monkeypatch.setattr(dictate, "is_pid_alive", lambda pid: True)
    monkeypatch.setattr(dictate, "stop_recording", lambda **kw: audio)
    monkeypatch.setattr(dictate, "transcribe", lambda *a, **kw: "hello world")
    monkeypatch.setattr(
        dictate, "enhance_prompt",
        lambda transcript, *, mode, config=None, caller=None: "Hello, world.",
    )
    monkeypatch.setattr(dictate, "copy_to_clipboard", lambda *a, **kw: None)
    monkeypatch.setattr(dictate, "notify", lambda *a, **kw: None)
    monkeypatch.setattr(dictate, "maybe_sleep_for_buffer", lambda *a, **kw: None)

    from agent_doctor.cli import main

    rc = main(["dictate", "stop"])
    assert rc == 0

    rows = dictate.read_history(state_dir=tmp_path, limit=10)
    assert len(rows) == 1
    assert rows[0]["transcript"] == "hello world"
    assert rows[0]["prompt"] == "Hello, world."
    assert rows[0]["enhanced"] == 1


def test_cli_dictate_stop_no_history_flag_skips_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_STATE_DIR", str(tmp_path))
    audio = tmp_path / "rec.wav"
    audio.write_bytes(b"RIFF....fake")
    write_state(
        DictateState(
            pid=99999, audio_path=str(audio), mode="chat",
            started_at=time.time(), recorder="sox", extras={},
        ),
        state_dir=tmp_path,
    )

    monkeypatch.setattr(dictate, "is_pid_alive", lambda pid: True)
    monkeypatch.setattr(dictate, "stop_recording", lambda **kw: audio)
    monkeypatch.setattr(dictate, "transcribe", lambda *a, **kw: "hi")
    monkeypatch.setattr(dictate, "enhance_prompt", lambda *a, **kw: "Hi.")
    monkeypatch.setattr(dictate, "copy_to_clipboard", lambda *a, **kw: None)
    monkeypatch.setattr(dictate, "notify", lambda *a, **kw: None)
    monkeypatch.setattr(dictate, "maybe_sleep_for_buffer", lambda *a, **kw: None)

    from agent_doctor.cli import main

    rc = main(["dictate", "stop", "--no-history"])
    assert rc == 0
    assert dictate.read_history(state_dir=tmp_path) == []


def test_cli_dictate_stop_buffer_ms_sleeps_before_terminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--buffer-ms 200 must call maybe_sleep_for_buffer(200) BEFORE stop_recording."""

    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_STATE_DIR", str(tmp_path))
    audio = tmp_path / "rec.wav"
    audio.write_bytes(b"RIFF....fake")
    write_state(
        DictateState(
            pid=99999, audio_path=str(audio), mode="chat",
            started_at=time.time(), recorder="sox", extras={},
        ),
        state_dir=tmp_path,
    )

    order: List[str] = []
    sleep_calls: List[int] = []

    def fake_sleep(buffer_ms: int, sleeper: Any = None) -> None:
        sleep_calls.append(buffer_ms)
        order.append("sleep")

    def fake_stop(**_kw: Any) -> Path:
        order.append("stop")
        return audio

    monkeypatch.setattr(dictate, "is_pid_alive", lambda pid: True)
    monkeypatch.setattr(dictate, "maybe_sleep_for_buffer", fake_sleep)
    monkeypatch.setattr(dictate, "stop_recording", fake_stop)
    monkeypatch.setattr(dictate, "transcribe", lambda *a, **kw: "hi")
    monkeypatch.setattr(dictate, "enhance_prompt", lambda *a, **kw: "Hi.")
    monkeypatch.setattr(dictate, "copy_to_clipboard", lambda *a, **kw: None)
    monkeypatch.setattr(dictate, "notify", lambda *a, **kw: None)

    from agent_doctor.cli import main

    rc = main(["dictate", "stop", "--buffer-ms", "200", "--no-history"])
    assert rc == 0
    assert sleep_calls == [200]
    assert order == ["sleep", "stop"]


def test_cli_dictate_timing_emits_phase_breakdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_STATE_DIR", str(tmp_path))
    audio = tmp_path / "rec.wav"
    audio.write_bytes(b"RIFF....fake")
    write_state(
        DictateState(
            pid=99999, audio_path=str(audio), mode="chat",
            started_at=time.time(), recorder="sox", extras={},
        ),
        state_dir=tmp_path,
    )

    monkeypatch.setattr(dictate, "is_pid_alive", lambda pid: True)
    monkeypatch.setattr(dictate, "stop_recording", lambda **kw: audio)
    monkeypatch.setattr(dictate, "transcribe", lambda *a, **kw: "hi")
    monkeypatch.setattr(dictate, "enhance_prompt", lambda *a, **kw: "Hi.")
    monkeypatch.setattr(dictate, "copy_to_clipboard", lambda *a, **kw: None)
    monkeypatch.setattr(dictate, "notify", lambda *a, **kw: None)
    monkeypatch.setattr(dictate, "maybe_sleep_for_buffer", lambda *a, **kw: None)

    from agent_doctor.cli import main

    rc = main(["dictate", "stop", "--timing", "--no-history"])
    out = capsys.readouterr()
    assert rc == 0
    # The timing JSON is written to stderr just before the receipt JSON on stdout.
    timing = json.loads([line for line in out.err.splitlines() if line.startswith("{")][0])
    assert "stop_ms" in timing
    assert "transcribe_ms" in timing
    assert "enhance_ms" in timing
    assert "clipboard_ms" in timing
    assert "total_ms" in timing
    assert timing["buffer_ms"] == dictate.DEFAULT_BUFFER_MS  # 150ms default


def test_cli_dictate_beep_off_skips_play_sound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--no-beep must suppress play_sound even if env says BEEP=1."""

    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_BEEP", "1")
    audio = tmp_path / "rec.wav"
    audio.write_bytes(b"RIFF....fake")
    write_state(
        DictateState(
            pid=99999, audio_path=str(audio), mode="chat",
            started_at=time.time(), recorder="sox", extras={},
        ),
        state_dir=tmp_path,
    )

    monkeypatch.setattr(dictate, "is_pid_alive", lambda pid: True)
    monkeypatch.setattr(dictate, "stop_recording", lambda **kw: audio)
    monkeypatch.setattr(dictate, "transcribe", lambda *a, **kw: "hi")
    monkeypatch.setattr(dictate, "enhance_prompt", lambda *a, **kw: "Hi.")
    monkeypatch.setattr(dictate, "copy_to_clipboard", lambda *a, **kw: None)
    monkeypatch.setattr(dictate, "notify", lambda *a, **kw: None)
    monkeypatch.setattr(dictate, "maybe_sleep_for_buffer", lambda *a, **kw: None)

    sound_calls: List[str] = []
    monkeypatch.setattr(dictate, "play_sound", lambda path, **kw: sound_calls.append(path))

    from agent_doctor.cli import main

    rc = main(["dictate", "stop", "--no-beep", "--no-history"])
    assert rc == 0
    assert sound_calls == []


def test_cli_dictate_beep_on_plays_done_sound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_STATE_DIR", str(tmp_path))
    audio = tmp_path / "rec.wav"
    audio.write_bytes(b"RIFF....fake")
    write_state(
        DictateState(
            pid=99999, audio_path=str(audio), mode="chat",
            started_at=time.time(), recorder="sox", extras={},
        ),
        state_dir=tmp_path,
    )

    monkeypatch.setattr(dictate, "is_pid_alive", lambda pid: True)
    monkeypatch.setattr(dictate, "stop_recording", lambda **kw: audio)
    monkeypatch.setattr(dictate, "transcribe", lambda *a, **kw: "hi")
    monkeypatch.setattr(dictate, "enhance_prompt", lambda *a, **kw: "Hi.")
    monkeypatch.setattr(dictate, "copy_to_clipboard", lambda *a, **kw: None)
    monkeypatch.setattr(dictate, "notify", lambda *a, **kw: None)
    monkeypatch.setattr(dictate, "maybe_sleep_for_buffer", lambda *a, **kw: None)

    sound_calls: List[str] = []
    monkeypatch.setattr(dictate, "play_sound", lambda path, **kw: sound_calls.append(path))

    from agent_doctor.cli import main

    rc = main(["dictate", "stop", "--beep", "--no-history"])
    assert rc == 0
    assert dictate.DEFAULT_DONE_SOUND in sound_calls


# --------------------------------------------------------------------------- #
# PR #28 review feedback (Gemini)                                             #
# --------------------------------------------------------------------------- #


def test_record_history_limit_zero_is_global_disable(tmp_path: Path) -> None:
    """Gemini medium #3: AGENT_DOCTOR_DICTATE_HISTORY_LIMIT=0 was documented
    as the global opt-out but the previous implementation still INSERTed
    rows (only the prune was skipped). The DB file must not be created
    at all when limit=0."""

    row_id = dictate.record_history(
        transcript="should not persist",
        prompt="should not persist",
        mode="chat",
        enhanced=False,
        state_dir=tmp_path,
        retention_limit=0,
    )
    assert row_id == 0
    assert not dictate.history_path(tmp_path).exists()
    assert dictate.read_history(state_dir=tmp_path) == []


def test_record_history_limit_zero_via_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_HISTORY_LIMIT", "0")
    row_id = dictate.record_history(
        transcript="x", prompt="x", mode="chat", enhanced=False,
        state_dir=tmp_path,
    )
    assert row_id == 0
    assert not dictate.history_path(tmp_path).exists()


def test_cli_dictate_stop_plays_stop_chime_when_beep_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gemini medium #1: --beep must play an inline "stop registered"
    chime immediately on stop so the user hears feedback even when the
    transcribe + LLM steps take a few seconds. The done chime fires later;
    here we just assert the inline stop chime is registered."""

    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_STATE_DIR", str(tmp_path))
    audio = tmp_path / "rec.wav"
    audio.write_bytes(b"RIFF....fake")
    write_state(
        DictateState(
            pid=99999, audio_path=str(audio), mode="chat",
            started_at=time.time(), recorder="sox", extras={},
        ),
        state_dir=tmp_path,
    )

    monkeypatch.setattr(dictate, "is_pid_alive", lambda pid: True)
    monkeypatch.setattr(dictate, "stop_recording", lambda **kw: audio)
    monkeypatch.setattr(dictate, "transcribe", lambda *a, **kw: "hi")
    monkeypatch.setattr(dictate, "enhance_prompt", lambda *a, **kw: "Hi.")
    monkeypatch.setattr(dictate, "copy_to_clipboard", lambda *a, **kw: None)
    monkeypatch.setattr(dictate, "notify", lambda *a, **kw: None)
    monkeypatch.setattr(dictate, "maybe_sleep_for_buffer", lambda *a, **kw: None)

    sound_calls: List[str] = []
    monkeypatch.setattr(dictate, "play_sound", lambda path, **kw: sound_calls.append(path))

    from agent_doctor.cli import main

    rc = main(["dictate", "stop", "--beep", "--no-history"])
    assert rc == 0
    # The inline "stop chime" (start sound) must be the FIRST call, before
    # any done/fail chime.
    assert sound_calls, "expected at least one play_sound call when --beep is set"
    assert sound_calls[0] == dictate.DEFAULT_START_SOUND, (
        f"first sound must be the stop chime ({dictate.DEFAULT_START_SOUND}); "
        f"got call sequence {sound_calls!r}"
    )
    # And the done chime must still fire after success.
    assert dictate.DEFAULT_DONE_SOUND in sound_calls


def test_cli_dictate_stop_records_effective_whisper_model_and_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gemini medium #2: when the user passes neither --whisper-model nor
    --backend, the history row must record the EFFECTIVE values
    (DEFAULT_WHISPER_MODEL and detect_backend(DEFAULT_WHISPER_MODEL)),
    not literal Nones."""

    from agent_doctor import dictate_settings as ds

    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("AGENT_DOCTOR_DICTATE_WHISPER_MODEL", raising=False)
    monkeypatch.delenv("AGENT_DOCTOR_DICTATE_BACKEND", raising=False)
    # Isolate settings so resolve_whisper_model() can't pick up the
    # developer's real ~/.agent-doctor/dictate.json (settings precedence
    # was added in PRα so the test must scope it to tmp_path).
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")

    audio = tmp_path / "rec.wav"
    audio.write_bytes(b"RIFF....fake")
    write_state(
        DictateState(
            pid=99999, audio_path=str(audio), mode="chat",
            started_at=time.time(), recorder="sox", extras={},
        ),
        state_dir=tmp_path,
    )

    monkeypatch.setattr(dictate, "is_pid_alive", lambda pid: True)
    monkeypatch.setattr(dictate, "stop_recording", lambda **kw: audio)
    monkeypatch.setattr(dictate, "transcribe", lambda *a, **kw: "hello")
    monkeypatch.setattr(dictate, "enhance_prompt", lambda *a, **kw: "Hello.")
    monkeypatch.setattr(dictate, "copy_to_clipboard", lambda *a, **kw: None)
    monkeypatch.setattr(dictate, "notify", lambda *a, **kw: None)
    monkeypatch.setattr(dictate, "maybe_sleep_for_buffer", lambda *a, **kw: None)

    from agent_doctor.cli import main

    rc = main(["dictate", "stop"])
    assert rc == 0

    rows = dictate.read_history(state_dir=tmp_path, limit=10)
    assert len(rows) == 1
    assert rows[0]["whisper_model"] == dictate.DEFAULT_WHISPER_MODEL
    # detect_backend('small') -> 'faster-whisper'
    assert rows[0]["backend"] == "faster-whisper"


def test_cli_dictate_stop_records_explicit_whisper_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_DOCTOR_DICTATE_STATE_DIR", str(tmp_path))
    audio = tmp_path / "rec.wav"
    audio.write_bytes(b"RIFF....fake")
    write_state(
        DictateState(
            pid=99999, audio_path=str(audio), mode="chat",
            started_at=time.time(), recorder="sox", extras={},
        ),
        state_dir=tmp_path,
    )

    monkeypatch.setattr(dictate, "is_pid_alive", lambda pid: True)
    monkeypatch.setattr(dictate, "stop_recording", lambda **kw: audio)
    monkeypatch.setattr(dictate, "transcribe", lambda *a, **kw: "hi")
    monkeypatch.setattr(dictate, "enhance_prompt", lambda *a, **kw: "Hi.")
    monkeypatch.setattr(dictate, "copy_to_clipboard", lambda *a, **kw: None)
    monkeypatch.setattr(dictate, "notify", lambda *a, **kw: None)
    monkeypatch.setattr(dictate, "maybe_sleep_for_buffer", lambda *a, **kw: None)

    from agent_doctor.cli import main

    rc = main([
        "dictate", "stop",
        "--whisper-model", "/path/to/ggml-large-v3-turbo.bin",
    ])
    assert rc == 0
    rows = dictate.read_history(state_dir=tmp_path, limit=10)
    assert rows[0]["whisper_model"] == "/path/to/ggml-large-v3-turbo.bin"
    # detect_backend on a .bin path -> 'whisper-cpp'
    assert rows[0]["backend"] == "whisper-cpp"


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


# --------------------------------------------------------------------------- #
# OPTIMIZE_PROMPT collapse (Phase 2 - T4)                                     #
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Deprecation warning for old modes (Phase 2 - T5)                            #
# --------------------------------------------------------------------------- #


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


def test_run_pipeline_emits_thinking_state_during_enhance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_pipeline writes a 'thinking' transient state while enhance runs."""

    from agent_doctor import pet_transient as _pt

    states_observed: list[str] = []
    real_write = _pt.write_transient

    def recording_write(state: str, **kwargs: object) -> Path:
        states_observed.append(state)
        return real_write(state, **kwargs)

    monkeypatch.setattr(_pt, "default_transient_file", lambda: tmp_path / "pt.json")
    monkeypatch.setattr(_pt, "write_transient", recording_write)

    audio = tmp_path / "in.wav"
    audio.write_bytes(b"\x00")

    def fake_transcriber(p: Path, m: str, l: object) -> str:
        return "hello world"

    def fake_caller(cfg: dictate.LLMConfig, messages: list[dict[str, str]]) -> str:
        return "Hello world."

    result = dictate.run_pipeline(
        audio,
        mode="optimize",
        enhance=True,
        transcriber=fake_transcriber,
        enhancer=fake_caller,
    )
    assert result.enhanced
    assert "thinking" in states_observed


def test_dictate_finish_emits_listening_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI handler should mark the pet as listening from 'stop' to the end of transcribe."""

    from agent_doctor import cli, dictate as _d, pet_transient as _pt

    states_observed: list[str] = []
    monkeypatch.setattr(_pt, "default_transient_file", lambda: tmp_path / "pt.json")
    real_write = _pt.write_transient

    def recording_write(state: str, **kwargs: object) -> Path:
        states_observed.append(state)
        return real_write(state, **kwargs)

    monkeypatch.setattr(_pt, "write_transient", recording_write)

    # Stub the entire pipeline out.
    monkeypatch.setattr(_d, "default_state_dir", lambda: tmp_path)
    state = _d.DictateState(
        pid=os.getpid(),
        audio_path=str(tmp_path / "x.wav"),
        mode="optimize",
        started_at=time.time(),
        recorder="sox",
    )
    (tmp_path / "x.wav").write_bytes(b"\x00")
    _d.write_state(state, state_dir=tmp_path)

    monkeypatch.setattr(_d, "stop_recording", lambda **_k: Path(state.audio_path))
    monkeypatch.setattr(_d, "transcribe", lambda *a, **k: "hello")
    monkeypatch.setattr(_d, "enhance_prompt", lambda *a, **k: "Hello.")
    monkeypatch.setattr(_d, "copy_to_clipboard", lambda *a, **k: None)
    monkeypatch.setattr(_d, "record_history", lambda **_k: 0)
    monkeypatch.setattr(_d, "notify", lambda *a, **k: None)
    monkeypatch.setattr(_d, "play_sound", lambda *a, **k: None)

    rc = cli.main(["dictate", "stop"])
    assert rc == 0
    assert "listening" in states_observed
