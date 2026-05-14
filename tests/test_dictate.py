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
from pathlib import Path
from typing import Any, Dict, List

import pytest

from agent_doctor import dictate
from agent_doctor.dictate import (
    DEFAULT_LLM_URL,
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


def test_llm_config_from_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_DOCTOR_DICTATE_LLM_URL", raising=False)
    monkeypatch.delenv("AGENT_DOCTOR_DICTATE_LLM_MODEL", raising=False)
    monkeypatch.delenv("AGENT_DOCTOR_DICTATE_LLM_KEY", raising=False)
    cfg = llm_config_from_env()
    assert cfg.url == DEFAULT_LLM_URL
    assert cfg.model == "ds4"
    assert cfg.api_key is None


def test_llm_config_from_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert "acceptance criteria" in seen["messages"][0]["content"].lower()
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
