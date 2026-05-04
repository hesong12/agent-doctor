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
