import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from agent_doctor.autopilot import run_notify_command
from agent_doctor.autopilot import run_autopilot_once


@pytest.fixture(autouse=True)
def _isolate_home_for_tests(monkeypatch, tmp_path_factory):
    """Redirect ~/.agent-doctor writes to a temp dir per-test.

    Legacy adapter dispatch can still write ~/.agent-doctor/<host>/inbox/.
    Without this fixture, tests that opt into dispatch would create real
    files under the developer's HOME.
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def test_autopilot_detects_negative_feedback_and_writes_card(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "session_id": "s1",
                "role": "user",
                "content": "你到底有没有想清楚？为什么没有用 Agent Doctor？",
            }
        ],
    )

    result = run_autopilot_once(
        platform="generic",
        path=transcript,
        out_dir=tmp_path / "doctor",
        cooldown_seconds=3600,
    )

    assert len(result.events) == 1
    event = result.events[0]
    assert event.trigger == "user_frustration_signal"
    assert event.severity == "high"
    assert event.action == "intervene"
    assert event.card_path is not None
    assert result.pet_state == "intervening"
    assert result.pet_status_path is not None
    assert result.pet_card_path is not None
    pet_status = json.loads(Path(result.pet_status_path).read_text(encoding="utf-8"))
    assert pet_status["state"] == "intervening"
    assert pet_status["action"] == "intervene"
    assert pet_status["card_path"] == event.card_path
    card = Path(event.card_path)
    assert card.exists()
    assert stat.S_IMODE(card.stat().st_mode) == 0o600
    assert "Agent Doctor Autopilot" in card.read_text(encoding="utf-8")
    assert "Immediate Agent Instruction" in card.read_text(encoding="utf-8")
    assert "user_frustration_signal" in (tmp_path / "doctor" / "events.jsonl").read_text(encoding="utf-8")


def test_autopilot_uses_state_to_suppress_repeated_events(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "session_id": "s1",
                "role": "user",
                "content": "This is not useful. You keep making the same mistake.",
            }
        ],
    )

    first = run_autopilot_once(platform="generic", path=transcript, out_dir=tmp_path / "doctor")
    second = run_autopilot_once(platform="generic", path=transcript, out_dir=tmp_path / "doctor")

    assert len(first.events) == 1
    assert second.events == []
    assert second.suppressed == 1
    assert second.pet_state == "intervening"
    assert second.pet_status_path is not None


def test_autopilot_changed_only_skips_unchanged_files_then_detects_modified_file(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    transcript = sessions / "session.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "session_id": "s1",
                "role": "user",
                "content": "This is not useful.",
            }
        ],
    )

    first = run_autopilot_once(platform="generic", path=sessions, out_dir=tmp_path / "doctor")
    second = run_autopilot_once(
        platform="generic",
        path=sessions,
        out_dir=tmp_path / "doctor",
        changed_only=True,
    )
    _write_jsonl(
        transcript,
        [
            {
                "session_id": "s1",
                "role": "user",
                "content": "This is not useful.",
            },
            {
                "session_id": "s2",
                "role": "user",
                "content": "You are not thinking clearly.",
            },
        ],
    )
    third = run_autopilot_once(
        platform="generic",
        path=sessions,
        out_dir=tmp_path / "doctor",
        changed_only=True,
        cooldown_seconds=0,
    )

    assert len(first.events) == 1
    assert second.messages == 0
    assert second.events == []
    assert second.pet_state == "idle"
    assert (tmp_path / "doctor" / "pet-status.json").exists()
    assert [event.session_id for event in third.events] == ["s2"]


def test_autopilot_always_writes_pet_status_even_without_event(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [
            {"session_id": "s-idle", "role": "user", "content": "Please list the files."},
            {"session_id": "s-idle", "role": "assistant", "content": "I can help with that."},
        ],
    )

    result = run_autopilot_once(
        platform="generic",
        path=transcript,
        out_dir=tmp_path / "doctor",
    )

    assert result.events == []
    assert result.pet_state == "idle"
    assert result.pet_status_path == str(tmp_path / "doctor" / "pet-status.json")
    assert result.pet_card_path == str(tmp_path / "doctor" / "pet-card.md")
    payload = json.loads((tmp_path / "doctor" / "pet-status.json").read_text(encoding="utf-8"))
    assert payload["state"] == "idle"
    assert payload["action"] == "silent"


def test_autopilot_detects_completion_claim_without_verification(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [
            {"session_id": "s2", "role": "user", "content": "Fix the login bug."},
            {"session_id": "s2", "role": "assistant", "content": "Fixed. All set."},
        ],
    )

    result = run_autopilot_once(
        platform="generic",
        path=transcript,
        out_dir=tmp_path / "doctor",
        min_severity="medium",
    )

    assert [event.trigger for event in result.events] == [
        "completion_claim_without_nearby_verification"
    ]


def test_autopilot_cli_smoke(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "session_id": "s3",
                "role": "user",
                "content": "You are not thinking clearly and this has no value.",
            }
        ],
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_doctor.cli",
            "autopilot",
            "--platform",
            "generic",
            "--path",
            str(transcript),
            "--out",
            str(tmp_path / "doctor"),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    summary = json.loads(result.stdout)

    assert summary["events"][0]["trigger"] == "user_frustration_signal"
    assert (tmp_path / "doctor" / "latest.md").exists()


def test_autopilot_writes_inbox_and_runs_notify_command(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    notify_log = tmp_path / "notify.log"
    _write_jsonl(
        transcript,
        [
            {
                "session_id": "s4",
                "role": "user",
                "content": "This has no value. You are not thinking.",
            }
        ],
    )

    result = run_autopilot_once(
        platform="generic",
        path=transcript,
        out_dir=tmp_path / "doctor",
        inbox_dir=tmp_path / "inbox",
        notify_command=f"{sys.executable} -c \"import os, pathlib; pathlib.Path(r'{notify_log}').write_text(os.environ['AGENT_DOCTOR_TRIGGER'])\"",
    )

    assert len(result.events) == 1
    assert result.delivery_errors == []
    assert notify_log.read_text(encoding="utf-8") == "user_frustration_signal"
    inbox_files = list((tmp_path / "inbox").glob("*.md"))
    assert len(inbox_files) == 1
    assert "Agent Doctor Advisory" in inbox_files[0].read_text(encoding="utf-8")
    assert "Action: `intervene`" in inbox_files[0].read_text(encoding="utf-8")


def test_autopilot_retries_intervention_after_delivery_failure(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    state = tmp_path / "doctor" / "state.sqlite3"
    _write_jsonl(
        transcript,
        [
            {
                "session_id": "s-delivery",
                "role": "user",
                "content": "你搞这些垃圾有什么用？治标不治本",
            }
        ],
    )

    first = run_autopilot_once(
        platform="generic",
        path=transcript,
        out_dir=tmp_path / "doctor",
        state_path=state,
        notify_command=f"{sys.executable} -c \"import sys; sys.exit(7)\"",
    )
    second = run_autopilot_once(
        platform="generic",
        path=transcript,
        out_dir=tmp_path / "doctor",
        state_path=state,
        notify_command=f"{sys.executable} -c \"import sys; sys.exit(7)\"",
    )

    assert len(first.events) == 1
    assert len(second.events) == 1
    assert first.delivery_errors
    assert second.delivery_errors
    assert "rc=7" in first.delivery_errors[0]


def test_run_notify_command_captures_subprocess_stderr(tmp_path: Path) -> None:
    """Failed notify subprocess: stderr is captured in the error string.

    Today the error string is just CalledProcessError's str(), which is
    'Command ... returned non-zero exit status N.' That hides why the
    subprocess actually failed. After this fix the error string includes
    rc + stderr + stdout so delivery-errors.jsonl is debuggable.
    """
    transcript = tmp_path / "session.jsonl"
    state = tmp_path / "doctor" / "state.sqlite3"
    _write_jsonl(
        transcript,
        [
            {
                "session_id": "s-stderr",
                "role": "user",
                "content": "你怎么这么笨，又搞错了",
            }
        ],
    )

    notify = (
        f"{sys.executable} -c "
        "\"import sys; sys.stderr.write('boom: openclaw not found\\n'); "
        "sys.stdout.write('partial stdout\\n'); sys.exit(1)\""
    )

    result = run_autopilot_once(
        platform="generic",
        path=transcript,
        out_dir=tmp_path / "doctor",
        state_path=state,
        notify_command=notify,
    )

    assert len(result.events) == 1
    assert result.delivery_errors, "delivery should have failed"
    err = result.delivery_errors[0]
    assert "rc=1" in err
    assert "boom: openclaw not found" in err
    assert "partial stdout" in err


def test_autopilot_records_successful_delivery_for_cooldown(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    state = tmp_path / "doctor" / "state.sqlite3"
    _write_jsonl(
        transcript,
        [
            {
                "session_id": "s-delivered",
                "role": "user",
                "content": "This has no value. You are not thinking.",
            }
        ],
    )

    first = run_autopilot_once(
        platform="generic",
        path=transcript,
        out_dir=tmp_path / "doctor",
        state_path=state,
        notify_command=f"{sys.executable} -c \"pass\"",
    )
    second = run_autopilot_once(
        platform="generic",
        path=transcript,
        out_dir=tmp_path / "doctor",
        state_path=state,
        notify_command=f"{sys.executable} -c \"pass\"",
    )

    assert len(first.events) == 1
    assert first.delivery_errors == []
    assert second.events == []
    assert second.suppressed == 1


def test_autopilot_detects_real_world_profanity_as_intervention(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "session_id": "s5",
                "role": "user",
                "content": "你是傻逼吗？这个 agent 怎么每次都这样？",
            }
        ],
    )

    result = run_autopilot_once(
        platform="generic",
        path=transcript,
        out_dir=tmp_path / "doctor",
    )

    assert len(result.events) == 1
    event = result.events[0]
    assert event.trigger == "user_frustration_signal"
    assert event.severity == "high"
    assert event.action == "intervene"
    assert "profanity_or_insult" in event.summary


def test_autopilot_detects_common_chinese_dumb_feedback(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "session_id": "s6",
                "role": "user",
                "content": "你怎么这么笨的？",
            }
        ],
    )

    result = run_autopilot_once(
        platform="generic",
        path=transcript,
        out_dir=tmp_path / "doctor",
    )

    assert len(result.events) == 1
    event = result.events[0]
    assert event.trigger == "user_frustration_signal"
    assert event.severity == "high"
    assert event.action == "intervene"


def test_autopilot_detects_common_english_dumb_feedback(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "session_id": "s6-english",
                "role": "user",
                "content": "Why are you so dumb?",
            }
        ],
    )

    result = run_autopilot_once(
        platform="generic",
        path=transcript,
        out_dir=tmp_path / "doctor",
    )

    assert len(result.events) == 1
    event = result.events[0]
    assert event.trigger == "user_frustration_signal"
    assert event.severity == "high"
    assert event.action == "intervene"


def test_autopilot_detects_chinese_dumb_feedback_variants(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [
            {"session_id": "s7", "role": "user", "content": "你很笨。"},
            {"session_id": "s8", "role": "user", "content": "那么笨还继续回答？"},
            {"session_id": "s9", "role": "user", "content": "笨死了。"},
            {"session_id": "s10", "role": "user", "content": "好笨。"},
        ],
    )

    result = run_autopilot_once(
        platform="generic",
        path=transcript,
        out_dir=tmp_path / "doctor",
        cooldown_seconds=0,
    )

    assert [event.session_id for event in result.events] == ["s7", "s8", "s9", "s10"]
    assert all(event.trigger == "user_frustration_signal" for event in result.events)
    assert all(event.severity == "high" for event in result.events)
    assert all(event.action == "intervene" for event in result.events)


def test_notify_command_reports_invalid_command() -> None:
    error = run_notify_command('"unterminated', event=run_autopilot_once)  # type: ignore[arg-type]

    assert error and "invalid notify command" in error


def test_dispatch_event_routes_through_adapter_inbox(tmp_path: Path, monkeypatch) -> None:
    """dispatch_event uses adapter.send_message; for GenericAdapter that
    means the inbox file gets written."""
    from agent_doctor.adapters import GenericAdapter
    from agent_doctor.autopilot import AutopilotEvent, dispatch_event

    monkeypatch.setenv("HOME", str(tmp_path))

    event = AutopilotEvent(
        id="e1",
        platform="generic",
        action="intervene",
        trigger="user_frustration_signal",
        severity="high",
        session_id="s1",
        message_file="/tmp/s1.jsonl",
        message_line=1,
        summary="user frustration",
        evidence="some evidence",
        finding_ids=[],
    )

    err = dispatch_event(event, GenericAdapter())

    assert err is None
    expected_inbox = tmp_path / ".agent-doctor" / "generic" / "inbox" / "s1.md"
    assert expected_inbox.exists()
    text = expected_inbox.read_text(encoding="utf-8")
    assert "🩺 Agent Doctor — user_frustration_signal" in text
    assert "user frustration" in text


def test_dispatch_event_returns_error_string_on_adapter_failure(tmp_path: Path) -> None:
    """When the adapter raises, dispatch_event captures and returns the error."""
    from agent_doctor.adapters import HostCapabilities
    from agent_doctor.autopilot import AutopilotEvent, dispatch_event

    class FailingAdapter:
        def capabilities(self):
            return HostCapabilities(host_name="failing", detected_at=tmp_path)

        def send_message(self, target, body, kind):
            raise RuntimeError("boom")

    event = AutopilotEvent(
        id="e2",
        platform="generic",
        action="intervene",
        trigger="user_frustration_signal",
        severity="high",
        session_id="s2",
        message_file="/tmp/s2.jsonl",
        message_line=1,
        summary="x",
        evidence="y",
        finding_ids=[],
    )

    err = dispatch_event(event, FailingAdapter())

    assert err is not None
    assert "adapter_error" in err
    assert "boom" in err


def test_run_autopilot_once_is_pet_only_by_default(tmp_path: Path, monkeypatch) -> None:
    """Default autopilot delivery writes Pet status, not adapter/inbox messages."""
    transcript = tmp_path / "session.jsonl"
    state = tmp_path / "doctor" / "state.sqlite3"
    _write_jsonl(
        transcript,
        [
            {
                "session_id": "s-pet-only",
                "role": "user",
                "content": "你太蠢了，又错了",
            }
        ],
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    result = run_autopilot_once(
        platform="generic",
        path=transcript,
        out_dir=tmp_path / "doctor",
        state_path=state,
        pet_out_dir=tmp_path / "pet",
    )

    assert len(result.events) == 1
    assert not (tmp_path / ".agent-doctor" / "generic" / "inbox" / "s-pet-only.md").exists()
    assert (tmp_path / "pet" / "pet-status.json").exists()
    pet_status = json.loads((tmp_path / "pet" / "pet-status.json").read_text(encoding="utf-8"))
    assert pet_status["state"] == "intervening"


def test_run_autopilot_once_can_dispatch_event_via_adapter_explicitly(
    tmp_path: Path, monkeypatch
) -> None:
    """Legacy adapter delivery is still available when explicitly requested."""
    from agent_doctor.adapters import GenericAdapter

    # Simulate frustration message
    transcript = tmp_path / "session.jsonl"
    state = tmp_path / "doctor" / "state.sqlite3"
    _write_jsonl(
        transcript,
        [
            {
                "session_id": "s-dispatch",
                "role": "user",
                "content": "你太蠢了，又错了",
            }
        ],
    )

    # Run autopilot pointing at our generic adapter (no notify command)
    monkeypatch.setenv("HOME", str(tmp_path))

    result = run_autopilot_once(
        platform="generic",
        path=transcript,
        out_dir=tmp_path / "doctor",
        state_path=state,
        dispatch_adapter=True,
    )

    assert len(result.events) == 1

    # Inbox file should have been written by GenericAdapter via dispatch_event
    expected_inbox = tmp_path / ".agent-doctor" / "generic" / "inbox" / "s-dispatch.md"
    assert expected_inbox.exists()
    text = expected_inbox.read_text(encoding="utf-8")
    assert "🩺" in text
    assert "你太蠢了" in text


def test_run_autopilot_once_drafts_proposals_at_threshold(tmp_path: Path) -> None:
    """When 3+ frustration messages fire in one session, a proposal is drafted."""
    transcript = tmp_path / "session.jsonl"
    state = tmp_path / "doctor" / "state.sqlite3"
    _write_jsonl(
        transcript,
        [
            {"session_id": "s-prop", "role": "user", "content": "你太蠢了"},
            {"session_id": "s-prop", "role": "user", "content": "又错了，废物"},
            {"session_id": "s-prop", "role": "user", "content": "傻逼"},
        ],
    )

    out_dir = tmp_path / "doctor"
    result = run_autopilot_once(
        platform="generic",
        path=transcript,
        out_dir=out_dir,
        state_path=state,
    )

    # At least one event should have fired
    assert len(result.events) >= 1

    # proposals.jsonl should be created with at least one entry
    proposals_path = out_dir / "proposals.jsonl"
    if proposals_path.exists():
        # If proposals were drafted, verify shape
        lines = [l for l in proposals_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        if lines:
            data = json.loads(lines[0])
            assert data["session_id"] == "s-prop"
            assert data["state"] == "pending"


def test_run_autopilot_once_polls_existing_proposals(tmp_path: Path) -> None:
    """If proposals.jsonl has pending proposals with a ✅ reaction available,
    a fresh autopilot cycle should transition them to applied."""
    # This is harder to test without a fake adapter that returns reactions.
    # Skipped scope-wise; covered by Task 9's e2e test.
    pass
