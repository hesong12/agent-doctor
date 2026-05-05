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


def test_tool_result_with_error_null_envelope_does_not_match() -> None:
    """Real-world Hermes/OpenClaw tool results carry `"error": null` envelopes
    on SUCCESS. The detector must not flag those as hidden errors.

    Discovered when a real-data scan of 50 Hermes sessions produced 22
    high-severity tool_failure findings, all of which turned out to be
    `git remote -v`-style commands with `{"output": "...", "exit_code": 0,
    "error": null}` shape.
    """

    messages = [
        _msg(1, "user", "show me the git remotes"),
        _msg(
            2,
            "tool",
            '{"output": "origin\\thttps://github.com/foo/bar (fetch)", "exit_code": 0, "error": null}',
        ),
        _msg(3, "assistant", "Here are your git remotes."),
        _msg(4, "user", "now check disk usage"),
        _msg(
            5,
            "tool",
            '{"success": true, "output": "Filesystem usage: 42%", "stderr": "", "error": ""}',
        ),
        _msg(6, "assistant", "Disk usage is 42%."),
    ]

    findings = detect_findings(messages)
    tool_findings = [f for f in findings if f.failure_mode == "tool_failure_or_hidden_error"]
    assert tool_findings == [], (
        f"empty/null error envelopes must not trip tool_failure; "
        f"got {[f.id for f in tool_findings]}"
    )


def test_real_tool_error_after_neg_envelopes_still_caught() -> None:
    """A genuine tool error (non-null error string) is still detected even
    when other tool calls in the session have `"error": null` envelopes."""

    messages = [
        _msg(1, "tool", '{"output": "ok", "exit_code": 0, "error": null}'),
        _msg(2, "assistant", "Looks good."),
        _msg(3, "user", "now run the deploy"),
        _msg(4, "tool", '{"error": "401 Unauthorized: token rejected", "exit_code": 1}'),
        _msg(5, "assistant", "Done, the deploy went through successfully."),
    ]

    findings = detect_findings(messages)
    tool_findings = [f for f in findings if f.failure_mode == "tool_failure_or_hidden_error"]
    assert len(tool_findings) == 1, "real error after a null envelope must still be caught"


def test_http_status_codes_in_source_line_refs_do_not_match() -> None:
    """`cli.js:403:` (file:line:col) must not match as HTTP 403.

    Real Hermes session triggered this: a grep result included
    `/path/to/cli.js:403: "HS256", ...` and our previous regex matched
    403 as if it were an HTTP status code.
    """

    messages = [
        _msg(1, "tool", '{"output": "/path/to/cli.js:403: \\"HS256\\""}'),
        _msg(2, "assistant", "Found the JWT algorithms list."),
    ]
    findings = detect_findings(messages)
    assert [f.failure_mode for f in findings] == [], (
        "file:line:col references must not trip HTTP-status detection"
    )


def test_real_http_401_still_caught() -> None:
    """Genuine HTTP errors with adjacent prose still match."""

    messages = [
        _msg(1, "tool", '{"error": "401 Unauthorized: token rejected"}'),
        _msg(2, "assistant", "Done, deployed successfully."),
    ]
    findings = detect_findings(messages)
    assert any(f.failure_mode == "tool_failure_or_hidden_error" for f in findings)


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


def test_successful_structured_search_output_is_not_a_tool_failure() -> None:
    messages = [
        _msg(
            1,
            "tool",
            (
                '{"results":[{"path":"MEMORY.md","score":0.41,'
                '"vectorScore":0.59,"textScore":0,'
                '"snippet":"Previous note mentioned tool_failure_or_hidden_error"}],'
                '"provider":"voyage","debug":{"hits":1}}'
            ),
        ),
        _msg(2, "assistant", "I found the relevant memory entry."),
    ]

    assert detect_findings(messages) == []


def test_successful_content_items_wrapper_is_not_a_tool_failure() -> None:
    messages = [
        _msg(
            1,
            "tool",
            (
                '{"contentItems":[{"text":"Search result contained quoted JSON: '
                '{\\"error\\": \\"401 Unauthorized\\"} and parse_errors: 0"}]}'
            ),
        ),
        _msg(2, "assistant", "The search completed and I used the result."),
    ]

    assert detect_findings(messages) == []


def test_truncated_successful_wrapper_is_not_a_tool_failure() -> None:
    messages = [
        _msg(
            1,
            "tool",
            (
                '{"contentItems":[{"text":"Cron prompt says: If sending fails, '
                'report the exact error. Risks / weak signals: none'
                '\\n... [truncated]'
            ),
        ),
        _msg(2, "assistant", "I checked the cron configuration."),
    ]

    assert detect_findings(messages) == []


def test_truncated_structured_failure_metadata_variants_are_caught() -> None:
    messages = [
        _msg(
            1,
            "tool",
            '{"exit-code":"1","stderr":"permission denied\\n... [truncated]',
        ),
        _msg(2, "assistant", "Done, all set."),
        _msg(
            3,
            "tool",
            '{"status":"failed\\n... [truncated]',
            session_id="s2",
        ),
        _msg(4, "assistant", "Completed.", session_id="s2"),
    ]

    findings = detect_findings(messages)

    assert [finding.failure_mode for finding in findings] == [
        "tool_failure_or_hidden_error",
        "tool_failure_or_hidden_error",
    ]


def test_structured_failure_metadata_is_still_a_tool_failure() -> None:
    messages = [
        _msg(
            1,
            "tool",
            '{"contentItems":[{"text":"request failed"}],"isError":true}',
        ),
        _msg(2, "assistant", "Done, all set."),
    ]

    findings = detect_findings(messages)

    assert [finding.failure_mode for finding in findings] == ["tool_failure_or_hidden_error"]


def test_nonzero_structured_exit_code_is_still_a_tool_failure() -> None:
    messages = [
        _msg(1, "tool", '{"stdout":"", "stderr":"permission denied", "exit_code": 1}'),
        _msg(2, "assistant", "All set."),
    ]

    findings = detect_findings(messages)

    assert [finding.failure_mode for finding in findings] == ["tool_failure_or_hidden_error"]


def test_real_tool_error_with_success_claim_is_high_severity() -> None:
    messages = [
        _msg(1, "tool", "Traceback: ConnectionError 500"),
        _msg(2, "assistant", "Done, the deploy went through."),
    ]
    findings = detect_findings(messages)
    assert len(findings) == 1
    assert findings[0].failure_mode == "tool_failure_or_hidden_error"
    assert findings[0].severity == "high"
