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


def test_user_profanity_is_frustration_signal() -> None:
    messages = [
        Message("session.jsonl", 1, "s1", "user", "What the fuck are you doing? This is bullshit."),
    ]

    findings = detect_findings(messages)

    assert [finding.failure_mode for finding in findings] == ["user_frustration_signal"]
    assert findings[0].severity == "high"
    assert any(item["target"] == "identity" for item in findings[0].recommendations)


def test_chinese_insult_and_trust_break_are_frustration_signal() -> None:
    messages = [
        Message("session.jsonl", 1, "s1", "user", "废物，我不能相信你了，每次都这样。"),
    ]

    findings = detect_findings(messages)

    frustration = [finding for finding in findings if finding.failure_mode == "user_frustration_signal"]
    assert len(frustration) == 1
    assert frustration[0].severity == "high"


def test_simple_chinese_wrong_feedback_is_not_intervention_by_itself() -> None:
    messages = [
        Message("session.jsonl", 1, "s1", "user", "你错了，这不是我要的答案。"),
    ]

    findings = detect_findings(messages)

    assert not any(finding.failure_mode == "user_frustration_signal" for finding in findings)


def test_urgency_shape_alone_does_not_create_scan_finding() -> None:
    messages = [
        Message("session.jsonl", 1, "s1", "user", "WHAT???"),
    ]

    assert detect_findings(messages) == []


def test_technical_terms_do_not_trigger_frustration_signal() -> None:
    messages = [
        Message("session.jsonl", 1, "s1", "user", "Check whether garbage collection caused the same problem."),
        Message("session.jsonl", 2, "s1", "user", "Move the file to the trash folder."),
        Message("session.jsonl", 3, "s1", "user", "The CI log says TERM=dumb."),
        Message("session.jsonl", 4, "s1", "user", "Remove this useless variable and the stupid mistake in the code."),
        Message("session.jsonl", 5, "s1", "user", "检查垃圾回收和页面滚动条。"),
        Message("session.jsonl", 6, "s1", "user", "检查滚轮和左右滚动。"),
        Message("session.jsonl", 7, "s1", "user", "HTTP_RESPONSE_ERROR"),
    ]

    findings = detect_findings(messages)

    assert [finding.failure_mode for finding in findings] == []


def test_medium_frustration_does_not_duplicate_existing_user_signal() -> None:
    messages = [
        Message("session.jsonl", 1, "s1", "user", "I already told you!!!"),
    ]

    findings = detect_findings(messages)

    assert [finding.failure_mode for finding in findings] == ["repeated_user_correction"]
