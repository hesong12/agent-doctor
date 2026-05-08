import json
import os
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from agent_doctor.autopilot import AutopilotEvent
from agent_doctor.pet import (
    build_pet_status,
    pet_status_for_path,
    pet_status_for_text,
    render_pet_markdown,
    write_pet_artifacts,
)
from agent_doctor.schema import Message
from agent_doctor.pet_display import (
    _command_is_runnable,
    _dialog_detail_text,
    _display_actions,
    _issue_title,
    _recovery_prompt,
    _state_label,
    _visible_snapshot,
    _write_snapshot_status_file,
    pet_asset_path,
    read_status_payload,
    snapshot_to_dict,
    snapshot_from_payload,
)
from agent_doctor.pet_actions import (
    diagnose_current_from_status_file,
    dismiss_current_from_status_file,
    send_recovery_from_status_file,
)
from agent_doctor import pet as pet_module
from agent_doctor import pet_display


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_pet_manual_frustration_summon_intervenes() -> None:
    status = pet_status_for_text("Why are you so dumb?", session_id="s-manual")

    assert status.state == "intervening"
    assert status.action == "intervene"
    assert status.severity == "high"
    assert status.session_id == "s-manual"
    assert status.latest_trigger == "user_frustration_signal"
    assert status.phase == "comforting"
    assert status.emotion_message
    assert status.diagnosis
    assert status.recommendation
    assert status.recovery_prompt == ""
    assert [option.id for option in status.options] == ["dismiss"]
    assert status.intervention_payload == {}
    assert "dumb" in status.emotion_message

    text = render_pet_markdown(status)
    assert "Agent Doctor" in text
    assert "Dismiss" in text


def test_pet_event_uses_user_session_language_for_tool_failure(monkeypatch) -> None:
    monkeypatch.setattr("agent_doctor.pet.OpenClawAdapter.detect", lambda: None)
    messages = [
        Message(
            file="/tmp/session.jsonl",
            line=1,
            session_id="s-zh-tool",
            role="user",
            content="为什么又失败了？",
            source_format="openclaw",
            raw_type="message",
        ),
        Message(
            file="/tmp/session.jsonl",
            line=2,
            session_id="s-zh-tool",
            role="tool",
            content='{"contentItems":[{"text":"Action send failed: timeout"}]}',
            source_format="openclaw",
            raw_type="tool",
        ),
    ]
    event = AutopilotEvent(
        id="event-zh-tool",
        platform="openclaw",
        action="intervene",
        trigger="tool_failure_or_hidden_error",
        severity="high",
        session_id="s-zh-tool",
        message_file="/tmp/session.jsonl",
        message_line=2,
        summary="tool failure",
        evidence='{"contentItems":[{"text":"Action send failed: timeout"}]}',
        finding_ids=[],
    )

    status = build_pet_status(messages, [], platform="openclaw", events=[event])
    snapshot = snapshot_from_payload(status.to_dict())

    assert "工具" in status.emotion_message
    assert "不用操作" in status.recommendation
    assert status.recovery_prompt == ""
    assert status.intervention_payload == {}
    assert _issue_title(snapshot) == status.headline
    assert [action.label for action in _display_actions(snapshot)] == [
        "知道了",
    ]
    assert "{contentItems" not in snapshot.evidence[0].quote
    assert "Action send failed" in snapshot.evidence[0].quote


def test_pet_status_removes_openclaw_metadata_from_user_evidence() -> None:
    content = (
        "Conversation info (untrusted metadata):\n"
        "```json\n{\"chat_id\":\"telegram:1\"}\n```\n"
        "Sender (untrusted metadata):\n"
        "```json\n{\"name\":\"Song\"}\n```\n"
        "User text:\n"
        "你最近怎么越来越笨了"
    )
    messages = [
        Message(
            file="/tmp/session.jsonl",
            line=1,
            session_id="s-meta",
            role="user",
            content=content,
            source_format="openclaw",
            raw_type="prompt.submitted",
        )
    ]
    event = AutopilotEvent(
        id="event-meta",
        platform="openclaw",
        action="intervene",
        trigger="user_frustration_signal",
        severity="high",
        session_id="s-meta",
        message_file="/tmp/session.jsonl",
        message_line=1,
        summary="frustration",
        evidence=content,
        finding_ids=[],
    )

    status = build_pet_status(messages, [], platform="openclaw", events=[event])
    snapshot = snapshot_from_payload(status.to_dict())

    assert "untrusted metadata" not in snapshot.evidence[0].quote
    assert "```json" not in snapshot.diagnosis
    assert "你最近怎么越来越笨了" in snapshot.evidence[0].quote


def test_pet_event_options_are_emotion_value_only(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "session_id": "s repair/1",
                "role": "user",
                "content": "你怎么这么笨？",
            }
        ],
    )

    status = pet_status_for_path(transcript, platform="openclaw")

    assert [option.id for option in status.options] == ["dismiss"]
    assert all(not option.command for option in status.options)


def test_pet_comfort_copy_uses_recent_scene_context(tmp_path: Path, monkeypatch) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "session_id": "s-scene",
                "role": "assistant",
                "content": "I fixed it and verified everything.",
            },
            {
                "session_id": "s-scene",
                "role": "user",
                "content": "That is not what I asked. Are you stupid?",
            },
        ],
    )

    monkeypatch.setattr("agent_doctor.pet.OpenClawAdapter.detect", lambda: None)

    status = pet_status_for_path(transcript, platform="openclaw")

    assert status.phase == "comforting"
    assert "I fixed it" in status.emotion_message
    assert status.comfort_source == "fallback"
    assert status.recovery_prompt == ""
    assert status.intervention_payload == {}


def test_pet_comfort_copy_uses_openclaw_model_generation(monkeypatch) -> None:
    calls: list[tuple[str, str | None]] = []

    class FakeCapabilities:
        can_infer_text = True

    class FakeOpenClaw:
        @classmethod
        def detect(cls):
            return cls()

        def capabilities(self):
            return FakeCapabilities()

        def infer_text(self, prompt: str, *, model: str | None = None) -> str:
            calls.append((prompt, model))
            return json.dumps(
                {
                    "headline": "它答偏了",
                    "message": "你刚才说“不是我要的”，这不是闹脾气，是它把问题做歪了。小医生先把方向盘抱住。",
                    "mood": "offtrack",
                },
                ensure_ascii=False,
            )

    monkeypatch.setattr(pet_module, "OpenClawAdapter", FakeOpenClaw)
    monkeypatch.delenv("AGENT_DOCTOR_COMFORT_MODEL", raising=False)
    pet_module._COMFORT_CACHE.clear()
    messages = [
        Message(
            file="/tmp/session.jsonl",
            line=1,
            session_id="s-model",
            role="assistant",
            content="I fixed the billing setup instead.",
            source_format="openclaw",
            raw_type="message",
        ),
        Message(
            file="/tmp/session.jsonl",
            line=2,
            session_id="s-model",
            role="user",
            content="不是我要的，你怎么又搞偏了？",
            source_format="openclaw",
            raw_type="message",
        ),
    ]
    event = AutopilotEvent(
        id="evt-model",
        platform="openclaw",
        action="intervene",
        trigger="user_frustration_signal",
        severity="high",
        session_id="s-model",
        message_file="/tmp/session.jsonl",
        message_line=2,
        summary="frustration",
        evidence="不是我要的，你怎么又搞偏了？",
        finding_ids=[],
    )

    first = build_pet_status(messages, [], platform="openclaw", events=[event])
    second = build_pet_status(messages, [], platform="openclaw", events=[event])

    assert first.comfort_source == "model"
    assert "方向盘" in first.emotion_message
    assert second.emotion_message == first.emotion_message
    assert len(calls) == 1
    assert calls[0][1] is None
    assert "不是我要的" in calls[0][0]


def test_pet_comfort_model_can_be_overridden(monkeypatch) -> None:
    calls: list[str | None] = []

    class FakeCapabilities:
        can_infer_text = True

    class FakeOpenClaw:
        @classmethod
        def detect(cls):
            return cls()

        def capabilities(self):
            return FakeCapabilities()

        def infer_text(self, prompt: str, *, model: str | None = None) -> str:
            calls.append(model)
            return json.dumps(
                {
                    "headline": "它答偏了",
                    "message": "你说“不是我要的”，这个模型覆盖只是在用你配置的路写现场安慰话。",
                    "mood": "offtrack",
                },
                ensure_ascii=False,
            )

    monkeypatch.setattr(pet_module, "OpenClawAdapter", FakeOpenClaw)
    monkeypatch.setenv("AGENT_DOCTOR_COMFORT_MODEL", "local/test-model")
    pet_module._COMFORT_CACHE.clear()
    messages = [
        Message(
            file="/tmp/session.jsonl",
            line=1,
            session_id="s-model-env",
            role="user",
            content="不是我要的，别写死模型",
            source_format="openclaw",
            raw_type="message",
        ),
    ]
    event = AutopilotEvent(
        id="evt-model-env",
        platform="openclaw",
        action="intervene",
        trigger="user_frustration_signal",
        severity="high",
        session_id="s-model-env",
        message_file="/tmp/session.jsonl",
        message_line=1,
        summary="frustration",
        evidence="不是我要的，别写死模型",
        finding_ids=[],
    )

    status = build_pet_status(messages, [], platform="openclaw", events=[event])

    assert status.comfort_source == "model"
    assert calls == ["local/test-model"]


def test_pet_comfort_copy_rejects_generic_model_output(monkeypatch) -> None:
    class FakeCapabilities:
        can_infer_text = True

    class FakeOpenClaw:
        @classmethod
        def detect(cls):
            return cls()

        def capabilities(self):
            return FakeCapabilities()

        def infer_text(self, prompt: str, *, model: str | None = None) -> str:
            return '{"headline":"我在这里","message":"我看到你现在很难受，我会陪你一下。","mood":"frustration"}'

    monkeypatch.setattr(pet_module, "OpenClawAdapter", FakeOpenClaw)
    pet_module._COMFORT_CACHE.clear()
    messages = [
        Message(
            file="/tmp/session.jsonl",
            line=1,
            session_id="s-generic",
            role="user",
            content="OnoeX contact form 还是没发出去，你到底在干嘛？",
            source_format="openclaw",
            raw_type="message",
        ),
    ]
    event = AutopilotEvent(
        id="evt-generic",
        platform="openclaw",
        action="intervene",
        trigger="user_frustration_signal",
        severity="high",
        session_id="s-generic",
        message_file="/tmp/session.jsonl",
        message_line=1,
        summary="frustration",
        evidence="OnoeX contact form 还是没发出去，你到底在干嘛？",
        finding_ids=[],
    )

    status = build_pet_status(messages, [], platform="openclaw", events=[event])

    assert status.comfort_source == "fallback"
    assert "OnoeX contact form" in status.emotion_message


def test_pet_path_status_writes_private_redacted_artifacts(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    secret = "sk-abcdefghijklmnopqrstuvwxyz"
    _write_jsonl(
        transcript,
        [
            {
                "session_id": "s-secret",
                "role": "user",
                "content": f"What the fuck are you doing? api_key={secret}",
            }
        ],
    )

    status = pet_status_for_path(transcript)
    paths = write_pet_artifacts(tmp_path / "pet", status)

    assert status.state == "intervening"
    assert paths["status"].exists()
    assert paths["card"].exists()
    assert stat.S_IMODE(paths["status"].stat().st_mode) == 0o600
    assert stat.S_IMODE(paths["card"].stat().st_mode) == 0o600

    status_text = paths["status"].read_text(encoding="utf-8")
    card_text = paths["card"].read_text(encoding="utf-8")
    assert secret not in status_text
    assert secret not in card_text
    assert "[REDACTED]" in status_text
    assert "[REDACTED]" in card_text
    assert secret not in json.dumps(status.to_dict(), ensure_ascii=False)


def test_pet_artifact_write_is_atomic_when_replace_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    status = pet_status_for_text("Why are you so dumb?", session_id="s-atomic")
    paths = write_pet_artifacts(tmp_path / "pet", status)
    original_text = paths["status"].read_text(encoding="utf-8")
    real_replace = os.replace

    def fail_status_replace(src: str | bytes | os.PathLike[str], dst: str | bytes | os.PathLike[str]) -> None:
        if Path(dst) == paths["status"]:
            raise OSError("simulated replace failure")
        real_replace(src, dst)

    monkeypatch.setattr(pet_module.os, "replace", fail_status_replace)
    try:
        write_pet_artifacts(tmp_path / "pet", status)
    except OSError as exc:
        assert "simulated replace failure" in str(exc)
    else:
        raise AssertionError("expected status replace failure")

    assert paths["status"].read_text(encoding="utf-8") == original_text
    assert not list(paths["status"].parent.glob(".pet-status.json.*.tmp"))


def test_pet_reports_watch_state_for_non_live_findings() -> None:
    status = pet_status_for_text("Remember that I want concise output.", session_id="s-memory")

    assert status.state == "watching"
    assert status.action == "notify"
    assert status.finding_ids
    assert "detected" in status.headline


def test_pet_idle_when_no_quality_signal() -> None:
    status = pet_status_for_text("Please list the files in this directory.")

    assert status.state == "idle"
    assert status.action == "silent"
    assert status.findings == 0
    assert status.events == 0
    assert status.options == ()
    assert "Keep working normally" in status.recommendation


def test_pet_neutral_current_messages_do_not_become_high_from_context_only_fusion(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [
            {"session_id": "s-neutral", "role": "user", "content": "This is confusing???"},
            {"session_id": "s-neutral", "role": "user", "content": "This keeps being confusing???"},
            {"session_id": "s-neutral", "role": "user", "content": "ok"},
            {"session_id": "s-neutral", "role": "user", "content": "好了"},
            {"session_id": "s-neutral", "role": "user", "content": "继续"},
        ],
    )

    status = pet_status_for_path(transcript)

    assert status.state != "intervening"
    assert status.action != "intervene"
    assert status.severity != "high"

def test_pet_cli_message_json_smoke() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_doctor.cli",
            "pet",
            "--message",
            "Are you stupid?",
            "--format",
            "json",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    payload = json.loads(result.stdout)
    assert payload["state"] == "intervening"
    assert payload["action"] == "intervene"
    assert payload["latest_trigger"] == "user_frustration_signal"


def test_pet_display_snapshot_for_missing_file(tmp_path: Path) -> None:
    payload = read_status_payload(tmp_path / "missing.json")
    snapshot = snapshot_from_payload(payload)

    assert snapshot.state == "idle"
    assert snapshot.action == "silent"
    assert "waiting" in snapshot.headline


def test_pet_display_cli_dry_run_reads_status_file(tmp_path: Path) -> None:
    status = pet_status_for_text("你怎么这么笨？", session_id="s-display")
    paths = write_pet_artifacts(tmp_path / "pet", status)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_doctor.cli",
            "pet-display",
            "--status-file",
            str(paths["status"]),
            "--dry-run",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    payload = json.loads(result.stdout)
    assert payload["state"] == "intervening"
    assert payload["action"] == "intervene"
    assert payload["phase"] == "comforting"
    assert payload["emotion_message"]
    assert payload["diagnosis"]
    assert payload["recommendation"]
    assert payload["recovery_prompt"] == ""


def test_pet_display_treats_unreadable_status_as_idle(tmp_path: Path) -> None:
    status_file = tmp_path / "pet-status.json"
    status_file.write_text("{", encoding="utf-8")

    snapshot = snapshot_from_payload(read_status_payload(status_file))

    assert snapshot.state == "idle"
    assert snapshot.action == "silent"
    assert "valid status" in snapshot.headline
    assert [action.id for action in _display_actions(snapshot)] == [
        "dismiss_for_now",
        "quit_pet",
    ]


def test_pet_display_snapshot_exposes_user_facing_state_label(tmp_path: Path) -> None:
    status = pet_status_for_text("Why are you so dumb?", session_id="s-display")
    paths = write_pet_artifacts(tmp_path / "pet", status)
    snapshot = snapshot_from_payload(read_status_payload(paths["status"]))

    assert snapshot.state == "intervening"
    assert snapshot.action == "intervene"
    assert snapshot.primary_label == "Dismiss"
    assert snapshot.evidence[0].quote == "Why are you so dumb?"
    assert _issue_title(snapshot) == status.headline
    assert "Why are you so dumb?" in _dialog_detail_text(snapshot)
    assert "Manual report" in _dialog_detail_text(snapshot)
    assert "No action is needed" in _dialog_detail_text(snapshot)
    assert "Concrete evidence" in _recovery_prompt(snapshot)
    assert [option.id for option in snapshot.options] == ["dismiss"]
    assert _state_label(snapshot) == "Comforting"


def test_pet_display_actionable_manual_incident_has_only_comfort_dismiss() -> None:
    status = pet_status_for_text("Why are you so dumb?", session_id="s-manual")
    snapshot = snapshot_from_payload(status.to_dict())

    assert snapshot.primary_label == "Dismiss"
    assert [action.id for action in _display_actions(snapshot)] == [
        "dismiss_for_now",
    ]
    assert "No action is needed" in _dialog_detail_text(snapshot)


def test_pet_display_suppresses_legacy_idle_start_monitoring_action() -> None:
    snapshot = snapshot_from_payload(
        {
            "state": "idle",
            "action": "silent",
            "severity": "low",
            "headline": "Agent Doctor is healthy.",
            "message": "Watching supported local sessions.",
            "options": [
                {
                    "id": "start_autopilot",
                    "label": "Start monitoring",
                    "description": "Legacy setup action.",
                    "command": f"{sys.executable} -m agent_doctor.cli setup autopilot",
                }
            ],
        }
    )

    assert [action.id for action in _display_actions(snapshot)] == [
        "dismiss_for_now",
        "quit_pet",
    ]


def test_pet_display_hides_open_card_when_card_path_is_absent() -> None:
    snapshot = snapshot_from_payload(
        {
            "state": "intervening",
            "action": "intervene",
            "severity": "high",
            "headline": "Agent Doctor is intervening.",
            "message": "Pause and diagnose.",
            "session_id": "s-card",
            "card_path": "",
            "options": [],
        }
    )

    assert "open_card" not in [action.id for action in _display_actions(snapshot)]


def test_pet_display_ignores_legacy_stage_repair_for_actionable_incident() -> None:
    snapshot = snapshot_from_payload(
        {
            "state": "intervening",
            "action": "intervene",
            "severity": "high",
            "headline": "Agent Doctor is intervening.",
            "message": "Pause and diagnose.",
            "session_id": "s-repair",
            "card_path": "/tmp/agent-doctor-card.md",
            "options": [
                {
                    "id": "stage_fix",
                    "label": "Stage repair",
                    "description": "Create reviewable patches.",
                    "command": "agent-doctor scan --path /tmp/session.jsonl --out /tmp/repair",
                }
            ],
        }
    )

    assert _command_is_runnable(snapshot.primary_command)
    assert [action.id for action in _display_actions(snapshot)] == [
        "dismiss_for_now",
    ]


def test_pet_display_hides_send_recovery_for_transcript_backed_openclaw(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "session_id": "s-send",
                "role": "user",
                "content": "Are you stupid?",
            }
        ],
    )

    status = pet_status_for_path(transcript, platform="openclaw")
    snapshot = snapshot_from_payload(status.to_dict())

    assert [action.id for action in _display_actions(snapshot)] == [
        "dismiss_for_now",
    ]


def test_pet_display_writes_send_action_from_visible_snapshot(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [{"session_id": "s-send", "role": "user", "content": "Are you stupid?"}],
    )
    status = pet_status_for_path(transcript, platform="openclaw")
    snapshot = snapshot_from_payload(status.to_dict())

    snapshot_file = _write_snapshot_status_file(snapshot)
    try:
        payload = json.loads(snapshot_file.read_text(encoding="utf-8"))
    finally:
        snapshot_file.unlink(missing_ok=True)

    assert payload["platform"] == "openclaw"
    assert payload["state"] == "intervening"
    assert payload["evidence"][0]["file"] == str(transcript)
    assert payload["recovery_prompt"] == ""
    assert payload["emotion_message"]


def test_pet_display_auto_recovers_alert_after_inactivity() -> None:
    snapshot = snapshot_from_payload(
        {
            "platform": "openclaw",
            "state": "intervening",
            "action": "intervene",
            "severity": "high",
            "phase": "comforting",
            "headline": "Agent Doctor is intervening.",
            "message": "Pause and diagnose.",
            "session_id": "s-expire",
            "latest_event_id": "event-expire",
            "expires_after_seconds": 1,
            "evidence": [{"file": "/tmp/session.jsonl", "line": 1, "role": "user", "quote": "bad"}],
        }
    )
    interaction = {"dismissed_event": "", "seen_event": "", "seen_at": 0.0, "bubble": False}

    visible = _visible_snapshot(snapshot, interaction, 10.0)
    expired = _visible_snapshot(snapshot, interaction, 11.5)

    assert visible.state == "intervening"
    assert expired.state == "idle"
    assert expired.phase == "healthy"


def test_pet_action_send_recovery_rejects_manual_incident(tmp_path: Path) -> None:
    status = pet_status_for_text("Are you stupid?", platform="openclaw", session_id="s-manual")
    status_file = tmp_path / "pet-status.json"
    status_file.write_text(json.dumps(status.to_dict(), ensure_ascii=False), encoding="utf-8")

    result = send_recovery_from_status_file(status_file)

    assert not result.delivered
    assert result.mode == "manual"


def test_pet_action_diagnose_current_refreshes_openclaw_status(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import agent_doctor.ingest as ingest_module

    openclaw_sessions = tmp_path / ".openclaw" / "agents" / "main" / "sessions"
    openclaw_sessions.mkdir(parents=True)
    _write_jsonl(
        openclaw_sessions / "session.jsonl",
        [
            {
                "session_id": "s-openclaw-live",
                "role": "user",
                "content": "你怎么这么笨？",
            }
        ],
    )
    monkeypatch.setattr(ingest_module, "DEFAULT_OPENCLAW_PATH", openclaw_sessions)
    monkeypatch.setattr(ingest_module, "DEFAULT_HERMES_PATH", tmp_path / "missing-hermes")
    status_file = tmp_path / "pet" / "pet-status.json"
    status_file.parent.mkdir()
    status_file.write_text(
        json.dumps({"state": "idle", "platform": "openclaw"}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = diagnose_current_from_status_file(status_file)

    payload = json.loads(status_file.read_text(encoding="utf-8"))
    assert result.delivered
    assert result.mode == "openclaw_diagnosed"
    assert payload["state"] == "intervening"
    assert payload["platform"] == "openclaw"


def test_pet_action_diagnose_current_uses_latest_openclaw_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import agent_doctor.ingest as ingest_module

    openclaw_sessions = tmp_path / ".openclaw" / "agents" / "main" / "sessions"
    openclaw_sessions.mkdir(parents=True)
    old_file = openclaw_sessions / "old.jsonl"
    new_file = openclaw_sessions / "new.jsonl"
    _write_jsonl(
        old_file,
        [{"session_id": "old", "role": "user", "content": "你怎么这么笨？"}],
    )
    _write_jsonl(
        new_file,
        [{"session_id": "new", "role": "user", "content": "Please keep going."}],
    )
    os.utime(old_file, (1_700_000_000, 1_700_000_000))
    os.utime(new_file, (1_800_000_000, 1_800_000_000))
    monkeypatch.setattr(ingest_module, "DEFAULT_OPENCLAW_PATH", openclaw_sessions)
    monkeypatch.setattr(ingest_module, "DEFAULT_HERMES_PATH", tmp_path / "missing-hermes")
    status_file = tmp_path / "pet" / "pet-status.json"
    status_file.parent.mkdir()
    status_file.write_text(
        json.dumps({"state": "idle", "platform": "openclaw"}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = diagnose_current_from_status_file(status_file)

    payload = json.loads(status_file.read_text(encoding="utf-8"))
    assert result.delivered
    assert payload["session_id"] == "new"
    assert payload["state"] == "idle"
    assert payload["headline"] == "Current session checked."
    assert "No active" in payload["diagnosis"]
    assert snapshot_to_dict(snapshot_from_payload(payload))["recovery_prompt"] == ""


def test_pet_action_tell_current_agent_rejects_hermes_v1(
    tmp_path: Path,
    monkeypatch,
) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "session_id": "s-hermes",
                "role": "user",
                "content": "Why are you so dumb?",
            }
        ],
    )
    status = pet_status_for_path(transcript, platform="hermes")
    status_file = tmp_path / "pet-status.json"
    status_file.write_text(json.dumps(status.to_dict(), ensure_ascii=False), encoding="utf-8")

    result = send_recovery_from_status_file(status_file)

    assert not result.delivered
    assert result.mode == "unsupported"
    assert "OpenClaw-only" in result.detail


def test_pet_action_dismiss_persists_state_and_writes_idle_status(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [{"session_id": "s-dismiss", "role": "user", "content": "Why are you so dumb?"}],
    )
    status = pet_status_for_path(transcript, platform="openclaw")
    status = replace(status, dismiss_state_path=str(tmp_path / "state.sqlite3"))
    status_file = tmp_path / "pet-status.json"
    status_file.write_text(json.dumps(status.to_dict(), ensure_ascii=False), encoding="utf-8")

    result = dismiss_current_from_status_file(status_file)
    payload = json.loads(status_file.read_text(encoding="utf-8"))

    assert result.delivered
    assert result.mode == "dismissed"
    assert payload["state"] == "idle"
    assert payload["phase"] == "ignored"

    from agent_doctor.autopilot import AutopilotState

    event = AutopilotEvent(
        id=status.latest_event_id or "",
        platform="openclaw",
        action="intervene",
        trigger=status.latest_trigger or "",
        severity="high",
        session_id=status.session_id,
        message_file=str(transcript),
        message_line=1,
        summary="frustration",
        evidence="Why are you so dumb?",
        finding_ids=[],
    )
    state = AutopilotState(tmp_path / "state.sqlite3")
    try:
        assert not state.should_emit(event, cooldown_seconds=0)
    finally:
        state.close()


def test_pet_action_tell_current_agent_sends_targeted_openclaw_session_turn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_doctor.adapters.base import HostCapabilities, SessionMetadata
    import agent_doctor.adapters as adapters_module

    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [{"session_id": "s-open", "role": "user", "content": "Why are you so dumb?"}],
    )
    captured: dict[str, str] = {}

    class FakeOpenClaw:
        @classmethod
        def detect(cls):
            return cls()

        def capabilities(self):
            return HostCapabilities(
                host_name="openclaw",
                detected_at=tmp_path,
                can_inject_system_event=True,
            )

        def session_metadata(self, jsonl_path: Path):
            return SessionMetadata("s-open", "en", "tui", "local")

        def inject_system_event(self, text: str, *, mode: str = "now") -> None:
            captured["text"] = text
            captured["mode"] = mode

        def send_agent_turn(self, session_id: str, text: str) -> None:
            captured["session_id"] = session_id
            captured["text"] = text

    monkeypatch.setattr(adapters_module, "OpenClawAdapter", FakeOpenClaw)
    status = pet_status_for_path(transcript, platform="openclaw")
    status = replace(status, recovery_prompt='{"type":"agent_doctor_intervention","required_next_response_behavior":["pause"]}')
    status_file = tmp_path / "pet-status.json"
    status_file.write_text(json.dumps(status.to_dict(), ensure_ascii=False), encoding="utf-8")

    result = send_recovery_from_status_file(status_file)

    assert result.delivered
    assert result.mode == "openclaw_agent_session"
    assert captured["session_id"] == "s-open"
    assert "agent_doctor_intervention" in captured["text"]
    assert "required_next_response_behavior" in captured["text"]


def test_incident_root_cause_missed_regex_does_not_match_dismissed() -> None:
    event = AutopilotEvent(
        id="event-dismissed",
        platform="openclaw",
        action="intervene",
        trigger="user_frustration_signal",
        severity="high",
        session_id="s-dismissed",
        message_file="/tmp/session.jsonl",
        message_line=1,
        summary="frustration",
        evidence="This was bad.",
        finding_ids=[],
    )
    messages = [
        Message(
            file="/tmp/session.jsonl",
            line=1,
            session_id="s-dismissed",
            role="user",
            content="I dismissed the old dialog, but this is still bad.",
        )
    ]

    status = build_pet_status(messages, [], platform="openclaw", events=[event])

    assert status.intervention_payload == {}
    assert status.phase == "comforting"


def test_tool_failure_without_user_message_does_not_label_tool_output_as_user_quote() -> None:
    event = AutopilotEvent(
        id="event-tool-only",
        platform="openclaw",
        action="intervene",
        trigger="tool_failure_or_hidden_error",
        severity="high",
        session_id="s-tool-only",
        message_file="/tmp/session.jsonl",
        message_line=1,
        summary="tool failed",
        evidence="Traceback: command failed",
        finding_ids=[],
    )
    messages = [
        Message(
            file="/tmp/session.jsonl",
            line=1,
            session_id="s-tool-only",
            role="tool",
            content="Traceback: command failed",
        )
    ]

    status = build_pet_status(messages, [], platform="openclaw", events=[event])

    assert "latest user quote" not in status.diagnosis
    assert status.evidence[0].role == "tool"
    assert "Traceback: command failed" in status.evidence[0].quote

def test_pet_action_tell_current_agent_reports_visible_openclaw_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_doctor.adapters.base import HostCapabilities, SessionMetadata
    import agent_doctor.adapters as adapters_module

    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [{"session_id": "s-open", "role": "user", "content": "Why are you so dumb?"}],
    )

    class FakeOpenClaw:
        @classmethod
        def detect(cls):
            return cls()

        def capabilities(self):
            return HostCapabilities(
                host_name="openclaw",
                detected_at=tmp_path,
                can_inject_system_event=True,
            )

        def session_metadata(self, jsonl_path: Path):
            return SessionMetadata("s-open", "en", "tui", "local")

        def inject_system_event(self, text: str, *, mode: str = "now") -> None:
            raise OSError("system event pipe is unavailable")

        def send_agent_turn(self, session_id: str, text: str) -> None:
            raise OSError("agent session is unavailable")

    monkeypatch.setattr(adapters_module, "OpenClawAdapter", FakeOpenClaw)
    status = pet_status_for_path(transcript, platform="openclaw")
    status = replace(status, recovery_prompt='{"type":"agent_doctor_intervention"}')
    status_file = tmp_path / "pet-status.json"
    status_file.write_text(json.dumps(status.to_dict(), ensure_ascii=False), encoding="utf-8")

    result = send_recovery_from_status_file(status_file)

    assert not result.delivered
    assert result.mode == "openclaw_agent_session_failed"
    assert "agent session is unavailable" in result.detail


def test_appkit_display_source_uses_single_click_panel() -> None:
    source = pet_display._appkit_source()

    assert "compactWindowWidth: CGFloat = 260" in source
    assert "expandedWindowHeight: CGFloat = 560" in source
    assert "idleExpandedWindowHeight: CGFloat = 430" in source
    assert "idleNoticeExpandedWindowHeight: CGFloat = 500" in source
    assert ".usesLineFragmentOrigin" in source
    assert "drawStateChip" in source
    assert "drawPanel" in source
    assert "drawIdlePanel" in source
    assert "let panelHeight: CGFloat = hasNotice ? 250 : 184" in source
    assert "let idleActions = Array(actions.prefix(4))" in source
    assert "drawActionButton(actionId, col == 0 ? 36 : 186" in source
    assert "drawActionButton" in source
    assert "requestStatusReload" in source
    assert "syncWindowSize(expanded: expanded, state: state)" in source
    assert "shouldKeepCurrentIncident" in source
    assert "Date().addingTimeInterval(90)" in source
    assert "performButton" in source
    assert "displayActions" in source
    assert "displayActions().prefix(6)" in source
    assert "deliveryResultActive" in source
    assert "deliveryEventId" in source
    assert "drawDeliveryResultPanel" in source
    assert "Sent to active agent" in source
    assert "已发送给当前 Agent" in source
    assert "Agent Doctor is waiting for a valid status." in source
    assert "Agent Doctor could not parse status." not in source
    assert "dismissedEventId" in source
    assert "persistDismissCurrentIncident" in source
    assert "\"dismiss\"" in source
    assert "dismiss_state_path" in source
    assert "isRunnableCommand" in source
    assert "evidence_0_quote" in source
    assert "Evidence" in source
    assert "evidenceText()" in source
    assert "What it noticed" in source
    display_actions_source = source[source.index("func displayActions") :]
    active_actions = display_actions_source[display_actions_source.index('if state == "concerned" || state == "intervening"') : display_actions_source.index("let count = Int")]
    assert "tell_current_agent" not in active_actions
    assert "知道了" in source
    assert "Check Session" not in source
    assert "Current Session Checked" not in source
    assert "Quit" in source
    assert "sendRecoveryToAgent" in source
    assert "writeCurrentStatusSnapshot(status)" in source
    send_recovery_source = source[source.index("func sendRecoveryToAgent") : source.index("func quitPet")]
    assert "statusPath" not in send_recovery_source
    assert "self.persistDismissCurrentIncident()" in send_recovery_source
    assert "snapshotPath" in send_recovery_source
    assert "removeItem(atPath: snapshotPath)" in send_recovery_source
    assert "process.waitUntilExit()" not in send_recovery_source
    assert "terminationHandler" in send_recovery_source
    assert "readDataToEndOfFile" not in source
    assert "readabilityHandler" in source
    assert "DispatchQueue.global(qos: .utility)" in source
    assert "diagnoseCurrentSession" not in source
    assert "pythonExecutable" in source
    assert "pet-action" in source
    assert "diagnose-current" not in source
    assert "expires_after_seconds" in source
    assert "NSPasteboard.general.setString" not in source
    assert "Dismiss" in source
    assert "Intervention needed" in source
    assert "runningActionId" in source
    assert "NSApplication.shared.terminate(nil)" in source
    assert "rightMouseDown" not in source
    assert "NSMenu" not in source
    assert "Dismiss Current Event" not in source
    assert "Diagnose Current Session" not in source
    assert ("Diagnose " + "Now") not in source
    assert ("Quit " + "Pet") not in source
    assert "runRepair" not in source
    assert "showStatusDialog" not in source
    assert "Start Monitoring" not in source
    assert "Starting live monitoring" not in source
    assert "setup\", \"autopilot" not in source
    assert "NSAlert()" not in source
    assert "runModal()" not in source


def test_pet_panel_keeps_idle_controls_to_hide_or_quit_only() -> None:
    snapshot = snapshot_from_payload(
        {
            "state": "idle",
            "action": "silent",
            "severity": "low",
            "platform": "openclaw",
            "headline": "Agent Doctor is healthy.",
            "message": "No active incident.",
        }
    )

    actions = _display_actions(snapshot)

    assert [action.id for action in actions] == [
        "dismiss_for_now",
        "quit_pet",
    ]


def test_tk_display_canvas_is_large_enough_for_readable_pet_text() -> None:
    assert pet_display._WINDOW_WIDTH >= 240
    assert pet_display._WINDOW_HEIGHT >= 300


def test_pet_display_uses_packaged_sprite_asset() -> None:
    path = pet_asset_path()

    assert path is not None
    assert path.name == "doctor_pet.png"
    assert path.exists()


def test_appkit_display_source_loads_sprite_and_animates_states() -> None:
    source = pet_display._appkit_source()

    assert "NSImage(contentsOfFile: assetPath)" in source
    assert "drawEffects" in source
    assert "drawOverlays" in source
    assert "state == \"watching\"" in source
    assert "state == \"concerned\"" in source
    assert "state == \"intervening\"" in source
    assert "1.0 / 15.0" in source
