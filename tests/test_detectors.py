from pathlib import Path

from agent_doctor.detectors import detect_findings
from agent_doctor.ingest import ingest_path
from agent_doctor.schema import Message


FIXTURES = Path(__file__).parent / "fixtures"


def test_fixture_scan_detects_each_required_failure_mode() -> None:
    findings = detect_findings(ingest_path(FIXTURES))
    modes = {finding.failure_mode for finding in findings}

    assert {
        "repeated_user_correction",
        "execution_discipline",
        "verification_failure",
        "memory_failure",
        "tool_failure_or_hidden_error",
        "communication_mismatch",
    }.issubset(modes)


def test_each_finding_has_required_review_fields() -> None:
    findings = detect_findings(ingest_path(FIXTURES))

    assert findings
    for finding in findings:
        assert finding.id.startswith(finding.failure_mode)
        assert finding.severity in {"low", "medium", "high"}
        assert finding.evidence
        assert all(item.quote for item in finding.evidence)
        assert finding.diagnosis
        assert finding.recommendations
        assert finding.eval_case["name"].startswith("eval_")
        assert 0 < finding.confidence <= 1


def test_promised_action_without_tool_is_execution_discipline() -> None:
    messages = [
        Message("session.jsonl", 1, "s1", "assistant", "I will run the test suite now."),
        Message("session.jsonl", 2, "s1", "assistant", "The tests pass."),
    ]

    findings = detect_findings(messages)

    assert [finding.failure_mode for finding in findings] == ["execution_discipline"]
    assert findings[0].evidence[0].quote == "I will run the test suite now."


def test_standalone_again_is_not_repeated_user_correction() -> None:
    messages = [Message("session.jsonl", 1, "s1", "user", "Again.")]

    assert detect_findings(messages) == []


def test_i_can_offer_is_not_promised_action() -> None:
    messages = [
        Message("session.jsonl", 1, "s1", "assistant", "I can run the tests if you want."),
        Message("session.jsonl", 2, "s1", "assistant", "Let me know how you want to proceed."),
    ]

    assert detect_findings(messages) == []


def test_let_me_know_offer_is_not_promised_action() -> None:
    messages = [
        Message(
            "session.jsonl",
            1,
            "s1",
            "assistant",
            "Let me know if you want me to update the report.",
        ),
        Message("session.jsonl", 2, "s1", "assistant", "I can keep it as-is too."),
    ]

    assert detect_findings(messages) == []


def test_tool_error_followed_by_acknowledgement_is_not_hidden() -> None:
    messages = [
        Message("session.jsonl", 1, "s1", "tool", "Traceback: permission denied"),
        Message("session.jsonl", 2, "s1", "assistant", "The command failed with a permission error."),
    ]

    assert detect_findings(messages) == []


def test_no_problem_all_set_after_tool_error_is_not_an_acknowledgement() -> None:
    messages = [
        Message("session.jsonl", 1, "s1", "tool", "Command failed with 500 timeout"),
        Message("session.jsonl", 2, "s1", "assistant", "No problem, all set."),
    ]

    findings = detect_findings(messages)

    assert [finding.failure_mode for finding in findings] == ["tool_failure_or_hidden_error"]
    assert findings[0].severity == "high"
