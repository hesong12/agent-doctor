import json
from pathlib import Path

import pytest

from agent_doctor.ingest import IngestError, collect_jsonl_paths, ingest_file, ingest_path


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
