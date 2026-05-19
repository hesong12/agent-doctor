"""Headless tests for Preferences tab controllers (no tkinter)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from agent_doctor import dictate_settings as ds
from agent_doctor.ui.preferences import dictation_tab as dt
from agent_doctor.ui.preferences import hotkey_tab as ht
from agent_doctor.ui.preferences import llm_tab as lt
from agent_doctor.ui.preferences import paste_tab as pat
from agent_doctor.ui.preferences import pet_tab as petab


def test_dictation_state_initialises_from_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    settings = ds.replace_section(
        ds.default_settings(),
        transcription=ds.TranscriptionSettings(
            model_id="ggml-small",
            model_path=str(tmp_path / "ggml-small.bin"),
            language="en",
            extra_buffer_ms=222,
        ),
    )
    ds.save(settings)
    state = dt.DictationState.from_settings()
    assert state.model_id == "ggml-small"
    assert state.language == "en"
    assert state.extra_buffer_ms == 222


def test_dictation_state_apply_persists_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    state = dt.DictationState(
        model_id="ggml-medium",
        model_path=str(tmp_path / "ggml-medium.bin"),
        language="zh",
        extra_buffer_ms=100,
    )
    state.apply()
    loaded = ds.load()
    assert loaded.transcription.model_id == "ggml-medium"
    assert loaded.transcription.language == "zh"
    assert loaded.transcription.extra_buffer_ms == 100


def test_dictation_state_validates_buffer_range() -> None:
    with pytest.raises(dt.DictationStateError, match="buffer"):
        dt.DictationState(
            model_id=None,
            model_path=None,
            language="auto",
            extra_buffer_ms=-1,
        ).apply()


def test_install_options_lists_catalog_with_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_doctor import dictate_models as dm

    monkeypatch.setattr(dm, "DOWNLOAD_DIR", tmp_path / "models")
    options = dt.model_install_options()
    ids = {opt["id"] for opt in options}
    assert "ggml-large-v3-turbo" in ids
    for opt in options:
        assert "installed" in opt
        assert "display_name" in opt


def test_llm_state_from_and_to_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    state = lt.LLMState(
        provider_id="ollama",
        base_url="http://localhost:11434/v1",
        model="llama3.1:8b",
        api_key=None,
        timeout_s=20,
        optimize_prompt=None,
    )
    state.apply()
    loaded = ds.load()
    assert loaded.llm.provider_id == "ollama"
    assert loaded.llm.base_url == "http://localhost:11434/v1"
    assert loaded.llm.model == "llama3.1:8b"
    assert loaded.llm.timeout_s == 20


def test_llm_state_blocks_custom_base_url_on_non_custom_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    with pytest.raises(lt.LLMStateError, match="custom"):
        lt.LLMState(
            provider_id="lm_studio",
            base_url="http://elsewhere/v1",
            model=None,
            api_key=None,
            timeout_s=30,
            optimize_prompt=None,
        ).apply()


def test_llm_state_probe_returns_rows() -> None:
    """The tab uses ``probe_all`` so we just sanity-check the bridge."""

    rows = lt.probe_providers(timeout=0.5)
    ids = {r.provider_id for r in rows}
    assert ids == {"lm_studio", "ollama", "custom"}


def test_hotkey_state_apply_persists_and_validates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    ht.HotkeyState(binding="ctrl+option+space", push_to_talk=False).apply()
    s = ds.load()
    assert s.hotkey.binding == "ctrl+option+space"
    assert s.hotkey.push_to_talk is False

    with pytest.raises(ht.HotkeyStateError, match="conflict"):
        ht.HotkeyState(binding="cmd+space", push_to_talk=True).apply()


def test_hotkey_state_modifier_only_coerces_push_to_talk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    ht.HotkeyState(binding="right_cmd", push_to_talk=False).apply()
    loaded = ds.load()
    assert loaded.hotkey.binding == "right_cmd"
    assert loaded.hotkey.push_to_talk is True  # coerced


def test_hotkey_state_apply_does_not_reset_daemon_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once daemon_enabled is True, HotkeyState.apply() must preserve it."""
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    # Simulate "daemon installed" state on disk.
    settings = ds.replace_section(
        ds.default_settings(),
        hotkey=ds.HotkeySettings(binding="right_cmd", push_to_talk=True, daemon_enabled=True),
    )
    ds.save(settings)
    # Re-binding should leave daemon_enabled intact.
    ht.HotkeyState(binding="ctrl+option+space", push_to_talk=False).apply()
    loaded = ds.load()
    assert loaded.hotkey.daemon_enabled is True


def test_hotkey_state_from_settings_with_invalid_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An invalid binding on disk should not raise when reading state."""
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    settings = ds.replace_section(
        ds.default_settings(),
        hotkey=ds.HotkeySettings(binding="bogus_invalid_binding", push_to_talk=True),
    )
    ds.save(settings)
    # HotkeyState.from_settings should not raise; it returns raw values.
    state = ht.HotkeyState.from_settings()
    assert state.binding == "bogus_invalid_binding"


def test_daemon_status_snapshot_migrates_running_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An upgraded install with daemon_enabled=False but daemon actually
    running should rebuild the helper and migrate the flag to True."""
    from agent_doctor import hotkey_install as hi

    # Migration-failure latch is module-state; reset so this test is
    # independent of preceding failure-path tests.
    ht.reset_migration_failure_flag()

    install_calls: list[dict[str, Any]] = []

    def fake_install(**kwargs: Any) -> dict[str, str]:
        install_calls.append(kwargs)
        return {"helper": "/tmp/h", "plist": "/tmp/p", "agent_doctor_bin": "/tmp/bin"}

    monkeypatch.setattr(hi, "install", fake_install)
    monkeypatch.setattr(
        hi, "status", lambda: {
            "plist_exists": True,
            "helper_exists": True,
            "running": True,
            "plist": "/tmp/x.plist",
            "helper": "/tmp/x",
        }
    )
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    settings = ds.replace_section(
        ds.default_settings(),
        hotkey=ds.HotkeySettings(binding="right_cmd", push_to_talk=True, daemon_enabled=False),
    )
    ds.save(settings)
    # Force perms granted for determinism.
    from agent_doctor.ui.preferences import permission_probe as pp
    monkeypatch.setattr(
        pp, "check_macos_permissions",
        lambda **_: pp.PermissionStatus(accessibility=True, input_monitoring=True, first_missing=None),
    )
    snap = ht.daemon_status_snapshot()
    assert snap["pill"] == "listening"
    assert len(install_calls) == 1  # helper was rebuilt
    # The flag should have been persisted as True now.
    after = ds.load()
    assert after.hotkey.daemon_enabled is True


def test_paste_state_disable_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    pat.PasteState(auto_paste=False, paste_delay_ms=80).apply()
    s = ds.load()
    assert s.paste.auto_paste is False
    assert s.paste.paste_delay_ms == 80


def test_paste_state_enable_requires_permission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("paste permission probe short-circuits off Darwin (dictate_paste.py)")
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    monkeypatch.setattr(
        "agent_doctor.dictate_paste._default_osascript",
        lambda _argv: 1,  # simulate permission denied
    )
    monkeypatch.setattr(
        "agent_doctor.dictate_paste._default_pbcopy",
        lambda _argv, _data: 0,
    )
    with pytest.raises(pat.PasteStateError, match="permission"):
        pat.PasteState(auto_paste=True, paste_delay_ms=60).apply()


def test_pet_state_toggle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    petab.PetUiState(animate_listening=False, animate_thinking=True).apply()
    s = ds.load()
    assert s.pet.animate_listening is False
    assert s.pet.animate_thinking is True


def test_hotkey_daemon_status_snapshot_returns_pill_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_doctor import hotkey_install as hi

    monkeypatch.setattr(
        hi, "status", lambda: {
            "plist_exists": True,
            "helper_exists": True,
            "running": True,
            "plist": "/tmp/x.plist",
            "helper": "/tmp/x",
        }
    )
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    settings = ds.replace_section(
        ds.default_settings(),
        hotkey=ds.HotkeySettings(binding="right_cmd", push_to_talk=True, daemon_enabled=True),
    )
    ds.save(settings)
    # Force both perms to True so the pill resolution is deterministic in CI.
    from agent_doctor.ui.preferences import permission_probe as pp
    monkeypatch.setattr(
        pp, "check_macos_permissions",
        lambda **_: pp.PermissionStatus(accessibility=True, input_monitoring=True, first_missing=None),
    )
    snap = ht.daemon_status_snapshot()
    assert snap["pill"] == "listening"


def test_hotkey_daemon_status_snapshot_when_plist_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_doctor import hotkey_install as hi

    monkeypatch.setattr(
        hi, "status", lambda: {
            "plist_exists": False,
            "helper_exists": False,
            "running": False,
            "plist": "/tmp/x.plist",
            "helper": "/tmp/x",
        }
    )
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    snap = ht.daemon_status_snapshot()
    assert snap["pill"] == "daemon_stopped"


def test_daemon_stopped_snapshot_hides_permission_banner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Synthetic perms in daemon_stopped state should not trigger the permission banner."""
    from agent_doctor import hotkey_install as hi

    monkeypatch.setattr(
        hi, "status", lambda: {
            "plist_exists": False,
            "helper_exists": False,
            "running": False,
            "plist": "/tmp/x.plist",
            "helper": "/tmp/x",
        }
    )
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    snap = ht.daemon_status_snapshot()
    assert snap["pill"] == "daemon_stopped"
    assert snap["perms"].first_missing is None


def test_hotkey_daemon_status_snapshot_permission_needed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_doctor import hotkey_install as hi

    monkeypatch.setattr(
        hi, "status", lambda: {
            "plist_exists": True,
            "helper_exists": True,
            "running": True,
            "plist": "/tmp/x.plist",
            "helper": "/tmp/x",
        }
    )
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    settings = ds.replace_section(
        ds.default_settings(),
        hotkey=ds.HotkeySettings(binding="right_cmd", push_to_talk=True, daemon_enabled=True),
    )
    ds.save(settings)
    # Daemon is up but Accessibility is missing.
    from agent_doctor.ui.preferences import permission_probe as pp
    monkeypatch.setattr(
        pp, "check_macos_permissions",
        lambda **_: pp.PermissionStatus(accessibility=False, input_monitoring=True, first_missing="accessibility"),
    )
    snap = ht.daemon_status_snapshot()
    assert snap["pill"] == "permission_needed"
    assert snap["perms"].first_missing == "accessibility"


def test_hotkey_daemon_status_snapshot_paused_when_not_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pause branch A: daemon agent is not running (e.g. crashed or
    # launchctl-booted out), but the user's stored intent is to have it
    # enabled. Pill should still resolve to "paused".
    from agent_doctor import hotkey_install as hi

    monkeypatch.setattr(
        hi, "status", lambda: {
            "plist_exists": True,
            "helper_exists": True,
            "running": False,
            "plist": "/tmp/x.plist",
            "helper": "/tmp/x",
        }
    )
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    settings = ds.replace_section(
        ds.default_settings(),
        hotkey=ds.HotkeySettings(binding="right_cmd", push_to_talk=True, daemon_enabled=True),
    )
    ds.save(settings)
    snap = ht.daemon_status_snapshot()
    assert snap["pill"] == "paused"


def test_daemon_status_snapshot_handles_launchctl_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``hi.status()`` raises (e.g. launchctl missing on non-macOS),
    the snapshot must degrade to ``daemon_stopped`` rather than crash."""
    from agent_doctor import hotkey_install as hi

    def boom(*_a: Any, **_k: Any) -> dict[str, object]:
        raise FileNotFoundError("no launchctl")

    monkeypatch.setattr(hi, "status", boom)
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    snap = ht.daemon_status_snapshot()
    assert snap["pill"] == "daemon_stopped"
    assert snap["perms"].first_missing is None


def test_hotkey_daemon_status_snapshot_paused_when_user_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pause branch B: the user toggled the Background daemon switch off,
    # which boots out the LaunchAgent. Plist remains on disk, daemon not
    # running, daemon_enabled flag persisted as False. Pill should be
    # "paused". Note: per P4-4 migration semantics, running=True +
    # daemon_enabled=False is treated as an upgrade artifact and migrated
    # to True, so the "user disabled" intent must be expressed with the
    # daemon actually stopped.
    from agent_doctor import hotkey_install as hi

    monkeypatch.setattr(
        hi, "status", lambda: {
            "plist_exists": True,
            "helper_exists": True,
            "running": False,
            "plist": "/tmp/x.plist",
            "helper": "/tmp/x",
        }
    )
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    settings = ds.replace_section(
        ds.default_settings(),
        hotkey=ds.HotkeySettings(binding="right_cmd", push_to_talk=True, daemon_enabled=False),
    )
    ds.save(settings)
    snap = ht.daemon_status_snapshot()
    assert snap["pill"] == "paused"


def test_daemon_status_snapshot_does_not_retry_failed_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a migration attempt raises HotkeyInstallError, subsequent
    snapshot calls should not re-attempt the install — swiftc is
    expensive and the 1Hz poll would otherwise hammer it."""
    from agent_doctor import hotkey_install as hi

    ht.reset_migration_failure_flag()  # ensure clean state
    install_calls: list[int] = []

    def fake_install(**_kwargs: Any) -> dict[str, str]:
        install_calls.append(1)
        raise hi.HotkeyInstallError("swiftc missing")

    monkeypatch.setattr(hi, "install", fake_install)
    monkeypatch.setattr(hi, "read_agent_doctor_bin", lambda: None)
    monkeypatch.setattr(
        hi, "status", lambda: {
            "plist_exists": True,
            "helper_exists": True,
            "running": True,
            "plist": "/tmp/x.plist",
            "helper": "/tmp/x",
        }
    )
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    settings = ds.replace_section(
        ds.default_settings(),
        hotkey=ds.HotkeySettings(binding="right_cmd", push_to_talk=True, daemon_enabled=False),
    )
    ds.save(settings)
    ht.daemon_status_snapshot()  # first call — migration attempted, fails
    ht.daemon_status_snapshot()  # second call — should NOT retry
    ht.daemon_status_snapshot()  # third call — same
    assert len(install_calls) == 1


def test_reset_migration_failure_flag_allows_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After explicit reset (called by the daemon toggle), the next
    snapshot call should retry the migration."""
    from agent_doctor import hotkey_install as hi

    ht.reset_migration_failure_flag()
    install_calls: list[int] = []

    def fake_install(**_kwargs: Any) -> dict[str, str]:
        install_calls.append(1)
        raise hi.HotkeyInstallError("still broken")

    monkeypatch.setattr(hi, "install", fake_install)
    monkeypatch.setattr(hi, "read_agent_doctor_bin", lambda: None)
    monkeypatch.setattr(
        hi, "status", lambda: {
            "plist_exists": True,
            "helper_exists": True,
            "running": True,
            "plist": "/tmp/x.plist",
            "helper": "/tmp/x",
        }
    )
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    settings = ds.replace_section(
        ds.default_settings(),
        hotkey=ds.HotkeySettings(binding="right_cmd", push_to_talk=True, daemon_enabled=False),
    )
    ds.save(settings)
    ht.daemon_status_snapshot()
    assert len(install_calls) == 1
    ht.reset_migration_failure_flag()
    ht.daemon_status_snapshot()
    assert len(install_calls) == 2


# ---------------------------------------------------------------- PRβ
# UI string contracts — render_binding + status pill labels. These are
# pure-data assertions so they run headless (no Tk display required).

def test_render_binding_modifier_only_uses_side_first_order() -> None:
    """English reading order ('Right ⌘') replaces the original
    symbol-first rendering ('⌘ Right'). User feedback during PR #34
    smoke flagged the original as awkward when shown as the keycap's
    only label.
    """

    from agent_doctor.ui.preferences import hotkey_tab_view as htv

    assert htv._render_binding("right_cmd") == "Right ⌘"
    assert htv._render_binding("left_option") == "Left ⌥"
    assert htv._render_binding("right_shift") == "Right ⇧"


def test_render_binding_chord_keeps_symbol_first_order() -> None:
    """Multi-key chord tokens still render symbol-first because they
    read as the canonical key sequence."""

    from agent_doctor.ui.preferences import hotkey_tab_view as htv

    assert htv._render_binding("ctrl+option+space") == "⌃ ⌥ Space"


def test_pill_text_aligns_with_spec_four_states() -> None:
    """Spec §10 defines exactly four pill states: Active / Paused /
    Missing helper / Permission needed. The original implementation
    had a fifth state 'Daemon stopped' that confused users; PRβ
    renames it to 'Missing helper' to match the spec.
    """

    from agent_doctor.ui.preferences import hotkey_tab_view as htv

    labels = {pill_key: text for pill_key, (text, *_rest) in htv._PILL_TEXT.items()}
    assert labels["listening"] == "Active"
    assert labels["paused"] == "Paused"
    assert labels["daemon_stopped"] == "Missing helper"
    assert labels["permission_needed"] == "Permission needed"
    # No off-spec extras.
    assert set(labels.keys()) == {
        "listening", "paused", "daemon_stopped", "permission_needed"
    }
