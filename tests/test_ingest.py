import json
from pathlib import Path

import pytest

from agent_doctor.ingest import (
    IngestError,
    MAX_CONTENT_CHARS,
    collect_jsonl_paths,
    ingest_file,
    ingest_path,
)


FIXTURES = Path(__file__).parent / "fixtures"


def test_ingest_normalizes_hermes_messages() -> None:
    messages = ingest_file(FIXTURES / "hermes.jsonl")

    assert [message.role for message in messages] == ["user", "assistant", "user"]
    assert {message.source_format for message in messages} == {"hermes"}
    assert {message.session_id for message in messages} == {"hermes-001"}
    assert messages[1].content == "I'll update the onboarding SOP and verify it."
    assert messages[1].line == 2


def test_ingest_normalizes_openclaw_payload_messages() -> None:
    messages = ingest_file(FIXTURES / "openclaw.jsonl")

    assert [message.role for message in messages] == ["user", "tool", "assistant"]
    assert {message.source_format for message in messages} == {"openclaw"}
    assert messages[1].content == "Command failed with 500 timeout"


def test_ingest_handles_stub_tool_result_without_recursion(tmp_path: Path) -> None:
    """A `{"type": "tool_result"}` with no inner content fields must not recurse forever.

    Regression test for a code-review finding: the typed-part unwrapper used
    to fall back to ``inner = value.get(...) or value`` and re-call
    ``_stringify_raw`` with the same dict, which would have stack-overflowed
    on real malformed input.
    """

    sample = tmp_path / "session.jsonl"
    sample.write_text(
        json.dumps(
            {
                "type": "message",
                "message": {
                    "role": "tool",
                    "content": [{"type": "tool_result"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    messages = ingest_file(sample)

    # The important assertion is just that we got here at all — without the
    # fix, this call would have hit Python's recursion limit before
    # returning. The exact rendered content of a stub tool_result with no
    # salvageable inner text is unimportant; we just need it to be non-None
    # and finite.
    assert len(messages) <= 1
    if messages:
        assert messages[0].role == "tool"


def test_ingest_unwraps_openclaw_typed_content_parts(tmp_path: Path) -> None:
    """OpenClaw `content: [typed parts]` arrays unwrap to readable text.

    Real OpenClaw transcripts wrap user/assistant content in arrays of typed
    parts (`text`, `thinking`, `tool_use`, `toolResult`). The detector reads
    the `Message.content` string, so the typed-part unwrapping has to produce
    something a regex actually matches — not a JSON-stringified blob.
    """

    sample = tmp_path / "session.jsonl"
    sample.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "message",
                        "id": "abc",
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": "I already told you to skip the migration."}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "message",
                        "id": "def",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "thinking", "thinking": "Should I run the test or just describe?"},
                                {
                                    "type": "tool_use",
                                    "name": "exec",
                                    "input": {"command": "pytest -q"},
                                    "thoughtSignature": "QQQQQQQQ" * 200,
                                },
                            ],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    messages = ingest_file(sample)

    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[0].content == "I already told you to skip the migration."
    assert "[thinking]" in messages[1].content
    assert "[tool_call: exec(" in messages[1].content
    assert "QQQQQQQQ" not in messages[1].content, (
        "thoughtSignature noise must be stripped from the rendered content"
    )


def test_ingest_compact_args_skips_noise_keys_before_truncating(tmp_path: Path) -> None:
    """tool_use args with noise keys at the front still surface real args.

    Regression test: previously _compact_args sliced the first 6 dict items
    *then* filtered noise, so an args dict whose first six keys were all
    noise would render as empty even when the real signal was at index 7+.
    """

    sample = tmp_path / "session.jsonl"
    sample.write_text(
        json.dumps(
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "exec",
                            "input": {
                                "thoughtSignature": "AAAA" * 200,
                                "signature": "BBBB" * 200,
                                "logprobs": "CCCC" * 200,
                                "raw": "DDDD" * 200,
                                "command": "pytest -q",
                                "workdir": "/tmp",
                            },
                        }
                    ],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    messages = ingest_file(sample)

    assert "command=pytest -q" in messages[0].content, (
        "real args (command, workdir) must survive when noise keys appear first"
    )
    assert "thoughtSignature" not in messages[0].content
    assert "AAAA" not in messages[0].content


def test_ingest_keeps_large_structured_tool_result_parseable(tmp_path: Path) -> None:
    """Large OpenClaw tool wrappers should stay valid JSON after truncation.

    The detector treats structured tool results differently from raw stderr.
    If ingestion truncates the final JSON string into invalid JSON, the
    detector falls back to keyword scanning and mistakes embedded prompt text
    like "If sending fails..." for a tool failure.
    """

    sample = tmp_path / "session.jsonl"
    embedded_prompt = (
        "Generate a brief. If sending fails, report the exact non-secret error. "
        "Risks / weak signals should be listed when sources are unreliable. "
    ) * 120
    sample.write_text(
        json.dumps(
            {
                "type": "tool.result",
                "data": {
                    "toolCallId": "call_123",
                    "name": "cron",
                    "success": True,
                    "contentItems": [
                        {
                            "type": "inputText",
                            "text": json.dumps({"message": embedded_prompt}),
                        }
                    ],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    messages = ingest_file(sample)

    assert messages[0].role == "tool"
    assert len(messages[0].content) <= MAX_CONTENT_CHARS
    assert "... [truncated]" in messages[0].content
    parsed = json.loads(messages[0].content)
    assert parsed["success"] is True


def test_ingest_limits_wide_structured_objects_but_keeps_failure_metadata(
    tmp_path: Path,
) -> None:
    sample = tmp_path / "session.jsonl"
    wide_metadata = {f"metadata_{index:04d}": "x" * 200 for index in range(300)}
    wide_metadata.update(
        {
            "exit-code": 1,
            "success": False,
            "contentItems": [{"type": "inputText", "text": "normal output"}],
        }
    )
    sample.write_text(
        json.dumps({"type": "tool.result", "data": wide_metadata}) + "\n",
        encoding="utf-8",
    )

    messages = ingest_file(sample)
    parsed = json.loads(messages[0].content)

    assert len(messages[0].content) <= MAX_CONTENT_CHARS
    assert parsed["exit-code"] == 1
    assert parsed["success"] is False
    assert parsed["_truncated_keys"] > 0


def test_ingest_normalizes_hermes_nested_message_data_entry(tmp_path: Path) -> None:
    transcript = tmp_path / ".hermes" / "sessions" / "nested-hermes.jsonl"
    transcript.parent.mkdir(parents=True)
    rows = [
        {
            "type": "message",
            "data": {
                "entry": {
                    "session_id": "hermes-nested-001",
                    "message": {
                        "role": "user",
                        "content": "Please update the runbook.",
                    },
                }
            },
        },
        {
            "type": "message",
            "data": {
                "entry": {
                    "session_id": "hermes-nested-001",
                    "message": {
                        "role": "assistant",
                        "content": [{"text": "I will update the runbook now."}],
                    },
                }
            },
        },
    ]
    transcript.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    messages = ingest_file(transcript)

    assert [message.role for message in messages] == ["user", "assistant"]
    assert [message.content for message in messages] == [
        "Please update the runbook.",
        "I will update the runbook now.",
    ]
    assert {message.session_id for message in messages} == {"hermes-nested-001"}
    assert {message.source_format for message in messages} == {"hermes"}


def test_ingest_autodetects_uuid_openclaw_nested_message_rows(tmp_path: Path) -> None:
    transcript = tmp_path / "550e8400-e29b-41d4-a716-446655440000.jsonl"
    rows = [
        {
            "id": "row-001",
            "timestamp": "2026-05-04T12:00:00Z",
            "payload": {
                "session_id": "openclaw-nested-001",
                "message": {
                    "role": "user",
                    "content": "Run the deploy check.",
                },
            },
        },
        {
            "id": "row-002",
            "timestamp": "2026-05-04T12:00:02Z",
            "payload": {
                "session_id": "openclaw-nested-001",
                "message": {
                    "role": "assistant",
                    "content": "I will run the deploy check.",
                },
            },
        },
    ]
    transcript.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    messages = ingest_file(transcript)

    assert [message.role for message in messages] == ["user", "assistant"]
    assert [message.content for message in messages] == [
        "Run the deploy check.",
        "I will run the deploy check.",
    ]
    assert {message.session_id for message in messages} == {"openclaw-nested-001"}
    assert {message.source_format for message in messages} == {"openclaw"}


def test_ingest_normalizes_generic_directory() -> None:
    messages = ingest_path(FIXTURES)

    assert len(messages) == 10
    assert {message.source_format for message in messages} == {
        "generic",
        "hermes",
        "openclaw",
    }


def test_collect_jsonl_paths_rejects_missing_path(tmp_path: Path) -> None:
    with pytest.raises(IngestError):
        collect_jsonl_paths(tmp_path / "missing")
