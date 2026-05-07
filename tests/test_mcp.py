"""Tests for the MCP server surface.

The pure-Python tool handlers don't need the `mcp` SDK — they take a JSON-
shaped arguments dict and return a JSON string envelope. We exercise each
handler directly and additionally verify that the SDK glue degrades cleanly
when the optional extra isn't installed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_doctor import mcp as mcp_module
from agent_doctor.mcp import (
    SERVER_NAME,
    TOOL_DEFINITIONS,
    find_tool,
    placeholder_payload,
    tool_bench,
    tool_doctor_pet_intervene,
    tool_doctor_pet_status,
    tool_generate_corpus,
    tool_list_findings,
    tool_read_finding,
    tool_scan,
    tool_stage_patches,
)


FIXTURES = Path(__file__).parent / "fixtures"


def _decode(text: str) -> dict:
    return json.loads(text)


def test_tool_definitions_have_unique_names_and_schemas() -> None:
    names = [tool.name for tool in TOOL_DEFINITIONS]
    assert len(names) == len(set(names)) > 0
    for tool in TOOL_DEFINITIONS:
        assert tool.description.strip()
        schema = tool.input_schema
        assert schema["type"] == "object"
        assert "properties" in schema


def test_find_tool_returns_definition_or_none() -> None:
    assert find_tool("scan") is not None
    assert find_tool("nonsense") is None


def test_placeholder_payload_advertises_real_server() -> None:
    payload = placeholder_payload()
    assert payload["status"] == "ready"
    assert payload["transport"] == "stdio"
    assert payload["command"] == "agent-doctor mcp serve"
    tool_names = {tool["name"] for tool in payload["tools"]}
    assert {
        "scan",
        "list_findings",
        "read_finding",
        "bench",
        "stage_patches",
        "generate_corpus",
        "doctor_pet_status",
        "doctor_pet_intervene",
    } <= tool_names


def test_tool_scan_writes_artifacts_and_returns_findings(tmp_path: Path) -> None:
    out_dir = tmp_path / "scan"
    response = _decode(tool_scan({"out_dir": str(out_dir), "path": str(FIXTURES)}))

    assert response["ok"] is True
    assert response["scan_dir"] == str(out_dir)
    assert response["summary"]["messages"] == 10
    assert response["summary"]["sessions"] == 3
    assert response["summary"]["findings"] >= 6
    assert (out_dir / "findings.json").exists()
    assert (out_dir / "report.md").exists()
    assert response["findings"], "scan should return finding summaries inline"
    assert all("evidence_count" in finding for finding in response["findings"])


def test_tool_scan_rejects_both_path_and_host(tmp_path: Path) -> None:
    response = _decode(
        tool_scan({"out_dir": str(tmp_path / "scan"), "path": str(FIXTURES), "host": "hermes"})
    )
    assert response["ok"] is False
    assert "not both" in response["error"]


def test_tool_scan_requires_path_or_host(tmp_path: Path) -> None:
    response = _decode(tool_scan({"out_dir": str(tmp_path / "scan")}))
    assert response["ok"] is False


def test_tool_list_findings_returns_summaries(tmp_path: Path) -> None:
    out_dir = tmp_path / "scan"
    tool_scan({"out_dir": str(out_dir), "path": str(FIXTURES)})

    response = _decode(tool_list_findings({"scan_dir": str(out_dir)}))
    assert response["ok"] is True
    assert response["count"] >= 6
    sample = response["findings"][0]
    assert {"id", "severity", "failure_mode", "title"} <= set(sample)


def test_tool_list_findings_errors_when_missing(tmp_path: Path) -> None:
    response = _decode(tool_list_findings({"scan_dir": str(tmp_path / "missing")}))
    assert response["ok"] is False


def test_tool_read_finding_returns_full_evidence(tmp_path: Path) -> None:
    out_dir = tmp_path / "scan"
    scan_response = _decode(tool_scan({"out_dir": str(out_dir), "path": str(FIXTURES)}))
    target_id = scan_response["findings"][0]["id"]

    response = _decode(tool_read_finding({"scan_dir": str(out_dir), "finding_id": target_id}))
    assert response["ok"] is True
    finding = response["finding"]
    assert finding["id"] == target_id
    assert isinstance(finding["evidence"], list)


def test_tool_read_finding_errors_for_unknown_id(tmp_path: Path) -> None:
    out_dir = tmp_path / "scan"
    tool_scan({"out_dir": str(out_dir), "path": str(FIXTURES)})

    response = _decode(tool_read_finding({"scan_dir": str(out_dir), "finding_id": "no-such-id"}))
    assert response["ok"] is False


def test_tool_stage_patches_writes_only_to_staging_dir(tmp_path: Path) -> None:
    scan_dir = tmp_path / "scan"
    staging_dir = tmp_path / "staging"
    target_dir = tmp_path / "live-config"
    target_dir.mkdir()
    sentinel = target_dir / "sentinel.txt"
    sentinel.write_text("untouched", encoding="utf-8")

    tool_scan({"out_dir": str(scan_dir), "path": str(FIXTURES)})
    response = _decode(
        tool_stage_patches(
            {
                "findings_path": str(scan_dir),
                "staging_dir": str(staging_dir),
                "target_dir": str(target_dir),
            }
        )
    )

    assert response["ok"] is True
    assert response["staging_dir"] == str(staging_dir)
    assert any(path.endswith("DIFF.txt") for path in response["files_written"])
    assert sentinel.read_text(encoding="utf-8") == "untouched"


def test_tool_stage_patches_severity_filter(tmp_path: Path) -> None:
    scan_dir = tmp_path / "scan"
    staging_dir = tmp_path / "staging"
    tool_scan({"out_dir": str(scan_dir), "path": str(FIXTURES)})

    response = _decode(
        tool_stage_patches(
            {
                "findings_path": str(scan_dir),
                "staging_dir": str(staging_dir),
                "min_severity": "high",
            }
        )
    )
    assert response["ok"] is True
    assert response["skipped"] >= 0


def test_tool_stage_patches_rejects_bad_severity(tmp_path: Path) -> None:
    response = _decode(
        tool_stage_patches(
            {
                "findings_path": str(tmp_path / "anything"),
                "staging_dir": str(tmp_path / "staging"),
                "min_severity": "critical",
            }
        )
    )
    assert response["ok"] is False


def test_tool_generate_corpus_and_bench_round_trip(tmp_path: Path) -> None:
    cards_dir = FIXTURES / "cards"
    corpus_dir = tmp_path / "corpus"
    bench_dir = tmp_path / "bench"

    gen = _decode(tool_generate_corpus({"cards_dir": str(cards_dir), "out_dir": str(corpus_dir)}))
    assert gen["ok"] is True
    assert (corpus_dir / "INDEX.json").exists()

    response = _decode(tool_bench({"corpus_dir": str(corpus_dir), "out_dir": str(bench_dir)}))
    assert response["ok"] is True
    assert response["summary"]["transcripts"] > 0
    assert (bench_dir / "bench.json").exists()


def test_tool_bench_errors_for_missing_corpus(tmp_path: Path) -> None:
    response = _decode(
        tool_bench({"corpus_dir": str(tmp_path / "missing"), "out_dir": str(tmp_path / "bench")})
    )
    assert response["ok"] is False


def test_tool_doctor_pet_status_from_message(tmp_path: Path) -> None:
    response = _decode(
        tool_doctor_pet_status(
            {
                "message": "Why are you so dumb?",
                "session_id": "s-pet",
                "out_dir": str(tmp_path / "pet"),
            }
        )
    )

    assert response["ok"] is True
    status = response["status"]
    assert status["state"] == "intervening"
    assert status["action"] == "intervene"
    assert status["session_id"] == "s-pet"
    assert response["artifacts"]["status"].endswith("pet-status.json")
    assert (tmp_path / "pet" / "pet-card.md").exists()


def test_tool_doctor_pet_intervene_returns_recovery_options() -> None:
    response = _decode(tool_doctor_pet_intervene({"message": "This is useless."}))

    assert response["ok"] is True
    assert response["should_intervene"] is True
    option_ids = [option["id"] for option in response["intervention"]["options"]]
    assert option_ids == ["dismiss"]


def test_tool_doctor_pet_requires_one_input() -> None:
    response = _decode(tool_doctor_pet_status({"message": "bad", "path": str(FIXTURES)}))

    assert response["ok"] is False
    assert "exactly one" in response["error"]


def _has_mcp_sdk() -> bool:
    import importlib.util

    return importlib.util.find_spec("mcp") is not None


@pytest.mark.skipif(not _has_mcp_sdk(), reason="mcp extra not installed")
def test_real_mcp_session_lists_and_calls_tools(tmp_path: Path) -> None:
    """Drive build_server() through the in-memory client/server pair."""

    import asyncio

    from mcp.shared.memory import create_connected_server_and_client_session

    from agent_doctor.mcp import build_server

    async def _exchange() -> dict:
        server = build_server()
        async with create_connected_server_and_client_session(server) as session:
            await session.initialize()
            tool_list = await session.list_tools()
            tool_names = {tool.name for tool in tool_list.tools}

            scan_result = await session.call_tool(
                "scan", {"out_dir": str(tmp_path / "scan"), "path": str(FIXTURES)}
            )
            text = scan_result.content[0].text
            payload = json.loads(text)
            return {"tool_names": tool_names, "scan": payload}

    out = asyncio.run(_exchange())
    assert {"scan", "list_findings", "stage_patches"} <= out["tool_names"]
    assert out["scan"]["ok"] is True
    assert out["scan"]["summary"]["findings"] >= 6


def test_serve_without_mcp_extra_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the `mcp` SDK isn't importable, `serve()` raises a helpful RuntimeError."""

    import builtins

    real_import = builtins.__import__

    def _fake_import(name: str, *args, **kwargs):
        if name == "mcp" or name.startswith("mcp."):
            raise ImportError(f"No module named '{name}' (test stub)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    with pytest.raises(RuntimeError) as exc_info:
        mcp_module.serve()

    assert "agent-doctor[mcp]" in str(exc_info.value)


def test_server_name_constant() -> None:
    assert SERVER_NAME == "agent-doctor"
