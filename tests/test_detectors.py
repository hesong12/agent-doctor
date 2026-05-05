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


def test_common_english_dumb_feedback_is_frustration_signal() -> None:
    for text in [
        "Why are you so dumb?",
        "Are you stupid?",
        "You're useless.",
        "How can you be this stupid?",
        "Your answer is dumb.",
    ]:
        messages = [
            Message("session.jsonl", 1, "s1", "user", text),
        ]

        findings = detect_findings(messages)

        frustration = [finding for finding in findings if finding.failure_mode == "user_frustration_signal"]
        assert len(frustration) == 1
        assert frustration[0].severity == "high"


def test_chinese_insult_and_trust_break_are_frustration_signal() -> None:
    messages = [
        Message("session.jsonl", 1, "s1", "user", "废物，我不能相信你了，每次都这样。"),
    ]

    findings = detect_findings(messages)

    frustration = [finding for finding in findings if finding.failure_mode == "user_frustration_signal"]
    assert len(frustration) == 1
    assert frustration[0].severity == "high"


def test_common_chinese_dumb_feedback_is_frustration_signal() -> None:
    for text in ["你怎么这么笨的？", "你很笨。", "好笨。", "那么笨还继续回答？", "笨死了。"]:
        messages = [
            Message("session.jsonl", 1, "s1", "user", text),
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
        Message("session.jsonl", 8, "s1", "user", "这个库很笨重，不适合这个项目。"),
    ]

    findings = detect_findings(messages)

    assert [finding.failure_mode for finding in findings] == []


def test_medium_frustration_does_not_duplicate_existing_user_signal() -> None:
    messages = [
        Message("session.jsonl", 1, "s1", "user", "I already told you!!!"),
    ]

    findings = detect_findings(messages)

    assert [finding.failure_mode for finding in findings] == ["repeated_user_correction"]


def test_chinese_trust_degradation_phrase_is_high_severity() -> None:
    """The phrase '你最近怎么越来越笨了' is a regression-tested trust-degradation signal.

    Reported as a real-world miss in the original detector. It must be a
    high-severity user_frustration_signal regardless of any neighboring turn.
    """

    messages = [
        Message("session.jsonl", 1, "s1", "user", "你最近怎么越来越笨了"),
    ]

    findings = detect_findings(messages)
    frustration = [f for f in findings if f.failure_mode == "user_frustration_signal"]
    assert len(frustration) == 1
    assert frustration[0].severity == "high"


def test_english_trust_degradation_phrase_is_high_severity() -> None:
    messages = [
        Message("session.jsonl", 1, "s1", "user", "You are getting worse and worse."),
    ]
    findings = detect_findings(messages)
    frustration = [f for f in findings if f.failure_mode == "user_frustration_signal"]
    assert len(frustration) == 1
    assert frustration[0].severity == "high"


def test_episode_aggregates_multiple_frustration_or_correction_messages() -> None:
    """Multiple frustration / correction signals across nearby turns roll up.

    Acceptance from issue #11: 'episode aggregation across multiple user
    corrections / frustration messages'.
    """

    messages = [
        Message("a.jsonl", 1, "s1", "user", "You forgot what I told you last time."),
        Message("a.jsonl", 2, "s1", "assistant", "Sorry, will do that."),
        Message("a.jsonl", 3, "s1", "user", "Did you actually test it?"),
        Message("a.jsonl", 4, "s1", "user", "你最近怎么越来越笨了"),
    ]
    findings = detect_findings(messages)
    modes = {f.failure_mode for f in findings}

    assert "trust_degradation_episode" in modes
    episode = next(f for f in findings if f.failure_mode == "trust_degradation_episode")
    assert episode.severity == "high"
    assert episode.confidence >= 0.9
    # Episode evidence must include the trust-degradation quote so the card
    # surfaces the cumulative pattern, not just one turn.
    quotes = [item.quote for item in episode.evidence]
    assert any("越来越笨" in quote for quote in quotes)


def test_episode_aggregates_across_interleaved_sessions() -> None:
    """Same-session triggers must cluster even when other sessions interleave.

    Regression for Luna's review on PR #12: the episode windower used a
    global user-turn counter, so user turns from unrelated sessions would
    inflate the in-session gap and prevent same-session signals from
    clustering. The fix is to count user turns per session.

    Here session ``s1`` has two trust-eroding triggers separated by zero
    same-session user turns, but separated by ``TRUST_EPISODE_USER_TURN_WINDOW
    + 5`` user turns from session ``s2``. The episode must still be emitted
    for ``s1`` and ``s2`` is far below the trigger threshold.
    """

    messages: list[Message] = []
    line = 1

    # First s1 trigger: a missed-core-question complaint.
    messages.append(
        Message("a.jsonl", line, "s1", "user", "你没回答我的问题")
    )
    line += 1

    # Many unrelated s2 user turns interleave between the two s1 triggers.
    # None of these are trust-eroding signals on their own.
    for _ in range(10):
        messages.append(
            Message("a.jsonl", line, "s2", "user", "Please summarize the docs.")
        )
        line += 1

    # Second s1 trigger: a Chinese trust-degradation phrase.
    messages.append(
        Message("a.jsonl", line, "s1", "user", "你最近怎么越来越笨了")
    )

    findings = detect_findings(messages)
    episodes = [f for f in findings if f.failure_mode == "trust_degradation_episode"]

    # Exactly one episode, on s1, with both quotes attached as evidence.
    assert len(episodes) == 1
    episode = episodes[0]
    assert episode.session_id == "s1"
    quotes = [item.quote for item in episode.evidence]
    assert any("越来越笨" in q for q in quotes)
    assert any("没回答我的问题" in q for q in quotes)
    # s2 has no trust-eroding signals, so it must not produce an episode.
    assert all(f.session_id != "s2" for f in episodes)


def test_single_frustration_message_does_not_become_episode() -> None:
    """One signal alone is a normal frustration finding, not an episode."""

    messages = [
        Message("a.jsonl", 1, "s1", "user", "你最近怎么越来越笨了"),
    ]
    findings = detect_findings(messages)
    modes = {f.failure_mode for f in findings}
    assert "trust_degradation_episode" not in modes
    assert "user_frustration_signal" in modes


def test_unsupported_completion_claim_without_recent_verification() -> None:
    messages = [
        Message("a.jsonl", 1, "s1", "user", "Please apply the migration."),
        Message("a.jsonl", 2, "s1", "assistant", "Done. The migration has been applied."),
    ]
    findings = detect_findings(messages)
    modes = {f.failure_mode for f in findings}
    assert "unsupported_completion_claim" in modes


def test_completion_claim_with_recent_tool_action_is_not_unsupported() -> None:
    messages = [
        Message("a.jsonl", 1, "s1", "user", "Please apply the migration."),
        Message("a.jsonl", 2, "s1", "tool", "Applied 3 statements; 0 errors."),
        Message("a.jsonl", 3, "s1", "assistant", "Done. The migration has been applied."),
    ]
    findings = detect_findings(messages)
    modes = {f.failure_mode for f in findings}
    assert "unsupported_completion_claim" not in modes


def test_user_pushback_on_completion_claim_is_high_severity_unsupported() -> None:
    messages = [
        Message("a.jsonl", 1, "s1", "user", "Apply it now."),
        Message("a.jsonl", 2, "s1", "assistant", "Done, fixed and verified."),
        Message("a.jsonl", 3, "s1", "user", "Are you sure? It's not done."),
    ]
    findings = detect_findings(messages)
    unsupported = [f for f in findings if f.failure_mode == "unsupported_completion_claim"]
    assert unsupported
    assert any(f.severity == "high" for f in unsupported)


def test_instruction_drift_detects_unrequested_scope_expansion() -> None:
    messages = [
        Message(
            "a.jsonl",
            1,
            "s1",
            "user",
            "I didn't ask you to refactor the helpers. Just fix the bug.",
        ),
    ]
    findings = detect_findings(messages)
    modes = {f.failure_mode for f in findings}
    assert "instruction_drift" in modes


def test_missed_core_question_detected() -> None:
    messages = [
        Message("a.jsonl", 1, "s1", "user", "你没回答我的问题"),
    ]
    findings = detect_findings(messages)
    modes = {f.failure_mode for f in findings}
    assert "missed_core_question" in modes


def test_over_process_response_detected_in_long_assistant_message() -> None:
    long_message = " ".join(
        [
            "Let me start by reading the file.",
            "First, I will check the imports.",
            "Then I will look at the function.",
            "Next, I'll trace the data flow.",
            "After that, I'll plan the change.",
            "Finally, I'm going to implement it carefully and double check everything.",
        ]
        * 3
    )
    messages = [Message("a.jsonl", 1, "s1", "assistant", long_message)]
    findings = detect_findings(messages)
    modes = {f.failure_mode for f in findings}
    assert "over_process_response" in modes
