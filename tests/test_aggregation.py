"""Tests for the two-pass aggregation in detectors.py."""

from __future__ import annotations

from agent_doctor.detectors import detect_findings
from agent_doctor.schema import Message


def _msg(line: int, role: str, content: str, session_id: str = "s1") -> Message:
    return Message("session.jsonl", line, session_id, role, content)  # type: ignore[arg-type]


def test_repeated_user_corrections_aggregate_and_escalate_severity() -> None:
    messages = [
        _msg(1, "user", "I already told you, that is not what I asked."),
        _msg(2, "user", "I already told you, do the rollback note."),
        _msg(3, "user", "I already told you. You did it again."),
    ]

    findings = detect_findings(messages)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.failure_mode == "repeated_user_correction"
    assert finding.count == 3
    assert finding.severity == "high"  # 3+ matches escalates to high.
    assert len(finding.evidence) == 3


def test_two_distinct_modes_in_one_session_produce_two_findings() -> None:
    messages = [
        _msg(1, "user", "Did you actually test it?"),
        _msg(2, "user", "You forgot what I told you last time."),
    ]

    findings = detect_findings(messages)
    modes = {f.failure_mode for f in findings}
    assert modes == {"verification_failure", "memory_failure"}
    for finding in findings:
        assert finding.count == 1


def test_aggregation_keeps_separate_sessions_separate() -> None:
    messages = [
        _msg(1, "user", "I already told you", session_id="s1"),
        _msg(2, "user", "I already told you", session_id="s2"),
    ]

    findings = detect_findings(messages)
    assert len(findings) == 2
    sessions = {f.session_id for f in findings}
    assert sessions == {"s1", "s2"}
    for finding in findings:
        assert finding.count == 1


def test_communication_mismatch_warning_changes_with_count() -> None:
    single = detect_findings([_msg(1, "user", "Stop explaining, you are too verbose.")])
    repeated = detect_findings(
        [
            _msg(1, "user", "Stop explaining, you are too verbose."),
            _msg(2, "user", "Stop explaining please."),
        ]
    )

    assert single[0].count == 1
    assert any("do not overfit" in r["proposal"] for r in single[0].recommendations)
    assert repeated[0].count == 2
    assert any(
        "Multiple matches in this session support a memory candidate" in r["proposal"]
        for r in repeated[0].recommendations
    )


def test_remember_in_neutral_context_is_not_memory_failure() -> None:
    """Tightened detector: 'remember' in informational context should not fire."""

    messages = [_msg(1, "user", "Just so I remember the timeline, when did we deploy?")]
    assert detect_findings(messages) == []


def test_zero_errors_in_tool_output_is_not_a_tool_failure() -> None:
    messages = [
        _msg(1, "tool", "Build summary: 0 errors, no failures."),
        _msg(2, "assistant", "Created the file and confirmed it works."),
    ]

    assert detect_findings(messages) == []


def test_error_in_identifier_is_not_a_tool_failure() -> None:
    messages = [
        _msg(1, "tool", "Wrote /tmp/error_handler.py and error.log successfully."),
        _msg(2, "assistant", "All set."),
    ]
    assert detect_findings(messages) == []


def test_real_tool_error_with_success_claim_is_high_severity() -> None:
    messages = [
        _msg(1, "tool", "Traceback: ConnectionError 500"),
        _msg(2, "assistant", "Done, the deploy went through."),
    ]
    findings = detect_findings(messages)
    assert len(findings) == 1
    assert findings[0].failure_mode == "tool_failure_or_hidden_error"
    assert findings[0].severity == "high"
