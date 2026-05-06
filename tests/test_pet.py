import json
import stat
import subprocess
import sys
from pathlib import Path

from agent_doctor.pet import (
    pet_status_for_path,
    pet_status_for_text,
    render_pet_markdown,
    write_pet_artifacts,
)
from agent_doctor.pet_display import (
    _command_is_runnable,
    _display_actions,
    _state_label,
    pet_asset_path,
    read_status_payload,
    snapshot_from_payload,
)
from agent_doctor import pet_display


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_pet_manual_frustration_summon_intervenes() -> None:
    status = pet_status_for_text("Why are you so dumb?", session_id="s-manual")

    assert status.state == "intervening"
    assert status.action == "intervene"
    assert status.severity == "high"
    assert status.session_id == "s-manual"
    assert status.latest_trigger == "user_frustration_signal"
    assert [option.id for option in status.options] == [
        "pause_and_diagnose",
        "stage_fix",
        "keep_watching",
    ]

    text = render_pet_markdown(status)
    assert "Agent Doctor Pet" in text
    assert "Pause and diagnose" in text


def test_pet_path_status_writes_private_redacted_artifacts(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    secret = "sk-abcdefghijklmnopqrstuvwxyz"
    _write_jsonl(
        transcript,
        [
            {
                "session_id": "s-secret",
                "role": "user",
                "content": f"What the fuck are you doing? api_key={secret}",
            }
        ],
    )

    status = pet_status_for_path(transcript)
    paths = write_pet_artifacts(tmp_path / "pet", status)

    assert status.state == "intervening"
    assert paths["status"].exists()
    assert paths["card"].exists()
    assert stat.S_IMODE(paths["status"].stat().st_mode) == 0o600
    assert stat.S_IMODE(paths["card"].stat().st_mode) == 0o600

    status_text = paths["status"].read_text(encoding="utf-8")
    card_text = paths["card"].read_text(encoding="utf-8")
    assert secret not in status_text
    assert secret not in card_text
    assert "[REDACTED]" in status_text
    assert "[REDACTED]" in card_text
    assert secret not in json.dumps(status.to_dict(), ensure_ascii=False)


def test_pet_reports_watch_state_for_non_live_findings() -> None:
    status = pet_status_for_text("Remember that I want concise output.", session_id="s-memory")

    assert status.state == "watching"
    assert status.action == "notify"
    assert status.finding_ids
    assert "detected" in status.headline


def test_pet_idle_when_no_quality_signal() -> None:
    status = pet_status_for_text("Please list the files in this directory.")

    assert status.state == "idle"
    assert status.action == "silent"
    assert status.findings == 0
    assert status.events == 0


def test_pet_cli_message_json_smoke() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_doctor.cli",
            "pet",
            "--message",
            "Are you stupid?",
            "--format",
            "json",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    payload = json.loads(result.stdout)
    assert payload["state"] == "intervening"
    assert payload["action"] == "intervene"
    assert payload["latest_trigger"] == "user_frustration_signal"


def test_pet_display_snapshot_for_missing_file(tmp_path: Path) -> None:
    payload = read_status_payload(tmp_path / "missing.json")
    snapshot = snapshot_from_payload(payload)

    assert snapshot.state == "idle"
    assert snapshot.action == "silent"
    assert "waiting" in snapshot.headline


def test_pet_display_cli_dry_run_reads_status_file(tmp_path: Path) -> None:
    status = pet_status_for_text("你怎么这么笨？", session_id="s-display")
    paths = write_pet_artifacts(tmp_path / "pet", status)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_doctor.cli",
            "pet-display",
            "--status-file",
            str(paths["status"]),
            "--dry-run",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    payload = json.loads(result.stdout)
    assert payload["state"] == "intervening"
    assert payload["action"] == "intervene"


def test_pet_display_snapshot_exposes_user_facing_state_label(tmp_path: Path) -> None:
    status = pet_status_for_text("Why are you so dumb?", session_id="s-display")
    paths = write_pet_artifacts(tmp_path / "pet", status)
    snapshot = snapshot_from_payload(read_status_payload(paths["status"]))

    assert snapshot.state == "intervening"
    assert snapshot.action == "intervene"
    assert snapshot.primary_label == "Stage repair"
    assert [option.id for option in snapshot.options] == [
        "pause_and_diagnose",
        "stage_fix",
        "keep_watching",
    ]
    assert _state_label(snapshot) == "Intervention needed"


def test_pet_display_hides_manual_stage_repair_without_command() -> None:
    status = pet_status_for_text("Why are you so dumb?", session_id="s-manual")
    snapshot = snapshot_from_payload(status.to_dict())

    assert snapshot.primary_label == "Stage repair"
    assert snapshot.primary_command == ""
    assert "stage_fix" not in [action.id for action in _display_actions(snapshot)]


def test_pet_display_hides_open_card_when_card_path_is_absent() -> None:
    snapshot = snapshot_from_payload(
        {
            "state": "intervening",
            "action": "intervene",
            "severity": "high",
            "headline": "Doctor is intervening.",
            "message": "Pause and diagnose.",
            "session_id": "s-card",
            "card_path": "",
            "options": [],
        }
    )

    assert "open_card" not in [action.id for action in _display_actions(snapshot)]


def test_pet_display_shows_runnable_stage_repair_action() -> None:
    snapshot = snapshot_from_payload(
        {
            "state": "intervening",
            "action": "intervene",
            "severity": "high",
            "headline": "Doctor is intervening.",
            "message": "Pause and diagnose.",
            "session_id": "s-repair",
            "card_path": "/tmp/agent-doctor-card.md",
            "options": [
                {
                    "id": "stage_fix",
                    "label": "Stage repair",
                    "description": "Create reviewable patches.",
                    "command": "agent-doctor scan --path /tmp/session.jsonl --out /tmp/repair",
                }
            ],
        }
    )

    assert _command_is_runnable(snapshot.primary_command)
    assert [action.id for action in _display_actions(snapshot)] == [
        "stage_fix",
        "open_card",
        "dismiss_for_now",
    ]


def test_appkit_display_source_has_context_menu_quit() -> None:
    source = pet_display._appkit_source()

    assert "windowWidth: CGFloat = 260" in source
    assert "windowHeight: CGFloat = 310" in source
    assert ".usesLineFragmentOrigin" in source
    assert "drawStateChip" in source
    assert "rightMouseDown" in source
    assert "NSMenu" in source
    assert "Quit Doctor Pet" in source
    assert "Stage Repair" in source
    assert "runRepair" in source
    assert "terminate(nil)" in source
    assert "showStatusDialog" in source
    assert "displayActions" in source
    assert "dismissedEventId" in source
    assert "isRunnableCommand" in source
    assert "Click for details" in source
    assert "Intervention needed" in source


def test_tk_display_canvas_is_large_enough_for_readable_pet_text() -> None:
    assert pet_display._WINDOW_WIDTH >= 240
    assert pet_display._WINDOW_HEIGHT >= 300


def test_pet_display_uses_packaged_sprite_asset() -> None:
    path = pet_asset_path()

    assert path is not None
    assert path.name == "doctor_pet.png"
    assert path.exists()


def test_appkit_display_source_loads_sprite_and_animates_states() -> None:
    source = pet_display._appkit_source()

    assert "NSImage(contentsOfFile: assetPath)" in source
    assert "drawEffects" in source
    assert "drawOverlays" in source
    assert "state == \"watching\"" in source
    assert "state == \"concerned\"" in source
    assert "state == \"intervening\"" in source
    assert "1.0 / 15.0" in source
