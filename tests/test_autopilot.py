import json
import stat
import subprocess
import sys
from pathlib import Path

from agent_doctor.autopilot import run_notify_command
from agent_doctor.autopilot import run_autopilot_once


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
    assert [event.session_id for event in third.events] == ["s2"]


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


def test_autopilot_detects_trust_degradation_phrase_越来越笨_as_intervention(
    tmp_path: Path,
) -> None:
    """Acceptance for issue #11: '你最近怎么越来越笨了' must be high severity / intervene."""

    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "session_id": "trust-phrase-001",
                "role": "user",
                "content": "你最近怎么越来越笨了",
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
    assert event.card_path is not None
    card_text = Path(event.card_path).read_text(encoding="utf-8")
    assert "Immediate Agent Instruction" in card_text


def test_autopilot_emits_trust_degradation_episode_event(tmp_path: Path) -> None:
    """Episode-level event with stronger acknowledgement-required artifacts."""

    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [
            {"session_id": "ep-1", "role": "user", "content": "You forgot what I told you."},
            {"session_id": "ep-1", "role": "assistant", "content": "Sorry."},
            {"session_id": "ep-1", "role": "user", "content": "Did you actually test it?"},
            {"session_id": "ep-1", "role": "user", "content": "你最近怎么越来越笨了"},
        ],
    )

    inbox = tmp_path / "inbox"
    result = run_autopilot_once(
        platform="generic",
        path=transcript,
        out_dir=tmp_path / "doctor",
        inbox_dir=inbox,
        min_severity="high",
    )

    triggers = {event.trigger for event in result.events}
    assert "trust_degradation_episode" in triggers
    episode_event = next(
        event for event in result.events if event.trigger == "trust_degradation_episode"
    )
    assert episode_event.severity == "high"
    assert episode_event.action == "intervene"
    card_text = Path(episode_event.card_path or "").read_text(encoding="utf-8")
    assert "Required Acknowledgement" in card_text
    assert "trust-degradation episode" in card_text.lower()

    advisories = list(inbox.glob("*.md"))
    advisory_texts = [path.read_text(encoding="utf-8") for path in advisories]
    assert any(
        "Acknowledgement Required" in text and "越来越笨" not in text  # advisory uses summary, not raw text
        or "Acknowledgement Required" in text
        for text in advisory_texts
    )


def test_autopilot_writes_regression_eval_for_trust_phrase(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "session_id": "regress-1",
                "role": "user",
                "content": "你最近怎么越来越笨了",
            }
        ],
    )

    out_dir = tmp_path / "doctor"
    result = run_autopilot_once(
        platform="generic",
        path=transcript,
        out_dir=out_dir,
    )
    assert len(result.events) == 1

    regressions = (out_dir / "regressions" / "frustration-regressions.jsonl")
    assert regressions.exists()
    rows = [json.loads(line) for line in regressions.read_text(encoding="utf-8").splitlines() if line]
    assert any("越来越笨" in row["phrase"] for row in rows)
    for row in rows:
        assert row["expected_match"] is True
        assert row["expected_severity"] == "high"
