"""Tests for the local-first settings module.

Covers:
- Keyring backend write+read+clear, mocked so no system Keychain prompt fires.
- File-fallback round trip with mode-bit assertion (file 0600, parent 0700).
- Redaction: any SettingsError raised from the settings module must NOT
  contain the secret in its string representation.
- ``settings_status()`` reports the right backend with/without a stored key.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest

from agent_doctor import settings as settings_mod

_FAKE_KEY = "FAKEKEY-NOT-A-REAL-GEMINI-CRED-NEVER-LEAKED-1234567890"
_FAKE_KEY_2 = "FAKEKEY-OTHER-NOT-A-REAL-GEMINI-CRED-9876543210"


def _redirect_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the settings module's file backend at a tmp dir."""

    home = tmp_path / "agent-doctor-home"
    monkeypatch.setattr(settings_mod, "_CONFIG_DIR", home)
    monkeypatch.setattr(settings_mod, "_CONFIG_FILE", home / "config.toml")
    return home


class _FakeKeyring:
    """In-memory stand-in for the optional ``keyring`` module."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) in self.store:
            del self.store[(service, username)]
        else:
            raise RuntimeError("no such entry")


def _install_fake_keyring(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyring:
    fake = _FakeKeyring()
    monkeypatch.setattr(settings_mod, "_try_import_keyring", lambda: fake)
    return fake


def _disable_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings_mod, "_try_import_keyring", lambda: None)


def test_keyring_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _install_fake_keyring(monkeypatch)
    _redirect_config(tmp_path, monkeypatch)

    backend = settings_mod.store_gemini_key(_FAKE_KEY)

    assert backend is settings_mod.Backend.KEYRING
    # Persisted in our fake keyring under the documented service+username.
    assert (
        fake.store.get((settings_mod._KEYRING_SERVICE, settings_mod._KEYRING_USERNAME))
        == _FAKE_KEY
    )
    assert settings_mod.load_gemini_key() == _FAKE_KEY


def test_keyring_promotion_clears_file_backup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the file backend held a stale key from a prior install, a successful
    keyring write must purge it so the user only has one source of truth."""

    home = _redirect_config(tmp_path, monkeypatch)
    _disable_keyring(monkeypatch)

    settings_mod.store_gemini_key(_FAKE_KEY)
    config = home / "config.toml"
    assert config.exists()

    # Now keyring becomes available; the next write should land in keyring
    # AND remove the stale key from the file backend. Because the file only
    # held the [gemini] section, the safe outcome is to unlink it entirely
    # rather than truncate in place (which would leak an FD and corrupt the
    # file on a mid-write crash — see the review fix in _file_clear).
    fake = _install_fake_keyring(monkeypatch)
    settings_mod.store_gemini_key(_FAKE_KEY_2)

    assert fake.store[(settings_mod._KEYRING_SERVICE, settings_mod._KEYRING_USERNAME)] == _FAKE_KEY_2
    if config.exists():
        text = config.read_text(encoding="utf-8")
        assert _FAKE_KEY not in text
        assert _FAKE_KEY_2 not in text
    # Whichever shape the file ended up in, ``load_gemini_key`` must see
    # the keyring value, not the prior file value.
    assert settings_mod.load_gemini_key() == _FAKE_KEY_2


def test_file_fallback_mode_bits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _redirect_config(tmp_path, monkeypatch)
    _disable_keyring(monkeypatch)

    backend = settings_mod.store_gemini_key(_FAKE_KEY)

    assert backend is settings_mod.Backend.FILE
    config = home / "config.toml"
    assert config.exists()
    file_mode = stat.S_IMODE(config.stat().st_mode)
    assert file_mode == 0o600, f"config.toml mode must be 0600, got {oct(file_mode)}"
    parent_mode = stat.S_IMODE(home.stat().st_mode)
    assert parent_mode == 0o700, f"~/.agent-doctor mode must be 0700, got {oct(parent_mode)}"

    assert settings_mod.load_gemini_key() == _FAKE_KEY


def test_file_fallback_overwrite_keeps_strict_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pre-existing file with looser permissions must be re-locked on rewrite."""

    home = _redirect_config(tmp_path, monkeypatch)
    _disable_keyring(monkeypatch)

    home.mkdir(parents=True)
    config = home / "config.toml"
    config.write_text("[gemini]\napi_key = \"old\"\n", encoding="utf-8")
    os.chmod(config, 0o644)
    os.chmod(home, 0o755)

    settings_mod.store_gemini_key(_FAKE_KEY)

    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert stat.S_IMODE(home.stat().st_mode) == 0o700


def test_clear_removes_from_both_backends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _redirect_config(tmp_path, monkeypatch)
    fake = _install_fake_keyring(monkeypatch)

    # Keyring write happens; file should be empty.
    settings_mod.store_gemini_key(_FAKE_KEY)

    # Manually plant a stale entry in the file backend to simulate dual-state.
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(
        "[gemini]\napi_key = \"stale\"\n", encoding="utf-8"
    )

    cleared = settings_mod.clear_gemini_key()

    assert cleared is True
    assert settings_mod.load_gemini_key() is None
    assert (settings_mod._KEYRING_SERVICE, settings_mod._KEYRING_USERNAME) not in fake.store


def test_redact_secret_replaces_full_secret() -> None:
    text = f"backend complained about token {_FAKE_KEY} during write"
    redacted = settings_mod.redact_secret(text, _FAKE_KEY)
    assert _FAKE_KEY not in redacted
    assert "***REDACTED***" in redacted


def test_settings_error_redacts_key_in_str(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the keyring backend itself raises with the key in the message,
    SettingsError must surface a redacted version — never the raw secret."""

    home = _redirect_config(tmp_path, monkeypatch)

    class BrokenKeyring:
        def get_password(self, service: str, username: str) -> str | None:
            return None

        def set_password(self, service: str, username: str, password: str) -> None:
            raise RuntimeError(f"keyring backend exploded: tried key {password}")

        def delete_password(self, service: str, username: str) -> None:
            raise RuntimeError("nope")

    monkeypatch.setattr(settings_mod, "_try_import_keyring", lambda: BrokenKeyring())

    # _keyring_set raises SettingsError with the key already redacted; in
    # store_gemini_key we catch that and fall back to file. So to exercise
    # the redaction codepath directly we call _keyring_set ourselves.
    with pytest.raises(settings_mod.SettingsError) as excinfo:
        settings_mod._keyring_set(_FAKE_KEY)

    assert _FAKE_KEY not in str(excinfo.value)
    assert "***REDACTED***" in str(excinfo.value)


def test_store_gemini_key_falls_back_when_keyring_broken(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Broken keyring must transparently fall back to the file backend."""

    home = _redirect_config(tmp_path, monkeypatch)

    class BrokenKeyring:
        def get_password(self, *_a: Any, **_k: Any) -> str | None:
            return None

        def set_password(self, service: str, username: str, password: str) -> None:
            raise RuntimeError(f"backend is broken: {password}")

        def delete_password(self, *_a: Any, **_k: Any) -> None:
            return None

    monkeypatch.setattr(settings_mod, "_try_import_keyring", lambda: BrokenKeyring())

    backend = settings_mod.store_gemini_key(_FAKE_KEY)

    assert backend is settings_mod.Backend.FILE
    assert settings_mod.load_gemini_key() == _FAKE_KEY


def test_settings_status_reports_configured_without_printing_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect_config(tmp_path, monkeypatch)
    _install_fake_keyring(monkeypatch)
    settings_mod.store_gemini_key(_FAKE_KEY)

    status = settings_mod.settings_status()
    rendered = status.render()

    assert status.configured is True
    assert status.backend is settings_mod.Backend.KEYRING
    assert "configured" in rendered
    assert _FAKE_KEY not in rendered


def test_settings_status_not_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect_config(tmp_path, monkeypatch)
    _install_fake_keyring(monkeypatch)

    status = settings_mod.settings_status()

    assert status.configured is False
    assert "not configured" in status.render()


def test_emit_toml_escapes_quotes_and_backslashes() -> None:
    weird = 'has"quote\\and\nnewline'
    body = settings_mod._emit_toml(weird)
    # Must round-trip through tomllib without raising.
    import tomllib

    parsed = tomllib.loads(body)
    assert parsed["gemini"]["api_key"] == weird


def test_file_set_uses_atomic_replace_not_truncate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ``_file_set`` mid-write failure must NOT empty the existing file
    (the bug the gemini-code-assist HIGH/MEDIUM review caught).

    We seed an existing config, then make ``os.replace`` fail. The
    pre-fix code opened the destination ``O_WRONLY | O_TRUNC`` and would
    have left the file empty. The atomic-write fix writes to a sibling
    temp file first, so the original survives untouched on failure.
    """

    home = _redirect_config(tmp_path, monkeypatch)
    _disable_keyring(monkeypatch)

    # Seed an existing valid config.
    settings_mod.store_gemini_key(_FAKE_KEY)
    config = home / "config.toml"
    original_bytes = config.read_bytes()
    assert _FAKE_KEY in original_bytes.decode("utf-8")

    # Force os.replace to blow up on the next write.
    real_replace = os.replace

    def boom(src: str, dst: str) -> None:
        # Clean up the temp file ourselves so the assertion below isn't
        # noisy with stray .config-*.toml.tmp entries.
        try:
            os.unlink(src)
        except OSError:
            pass
        raise OSError("simulated mid-replace failure")

    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(settings_mod.SettingsError) as excinfo:
        settings_mod.store_gemini_key(_FAKE_KEY_2)

    # The original file is intact — same bytes, same mode, same key.
    assert config.read_bytes() == original_bytes
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    # The new key never landed on disk.
    assert _FAKE_KEY_2 not in config.read_text(encoding="utf-8")
    # Error string never echoed the failed-write key.
    assert _FAKE_KEY_2 not in str(excinfo.value)

    # Restore os.replace and confirm a follow-up write still works (no
    # state corruption from the failed attempt).
    monkeypatch.setattr(os, "replace", real_replace)
    settings_mod.store_gemini_key(_FAKE_KEY_2)
    assert settings_mod.load_gemini_key() == _FAKE_KEY_2


def test_file_set_does_not_leak_temp_files_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _redirect_config(tmp_path, monkeypatch)
    _disable_keyring(monkeypatch)
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)

    def boom(src: str, dst: str) -> None:
        raise OSError("simulated")

    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(settings_mod.SettingsError):
        settings_mod.store_gemini_key(_FAKE_KEY)

    # No leftover .config-*.toml.tmp in the parent dir.
    leftovers = [p for p in home.iterdir() if p.name.startswith(".config-")]
    assert leftovers == [], f"unexpected temp leftovers: {leftovers!r}"


def test_file_clear_unlinks_when_only_gemini_section(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _redirect_config(tmp_path, monkeypatch)
    _disable_keyring(monkeypatch)

    settings_mod.store_gemini_key(_FAKE_KEY)
    config = home / "config.toml"
    assert config.exists()

    cleared = settings_mod.clear_gemini_key()

    assert cleared is True
    assert not config.exists(), "gemini-only config file should be unlinked, not truncated"


def test_file_clear_preserves_unknown_sections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Forward-compat: a future co-tenant section in config.toml must
    survive ``clear_gemini_key``. The pre-review implementation truncated
    the entire file, which would have wiped unrelated settings."""

    home = _redirect_config(tmp_path, monkeypatch)
    _disable_keyring(monkeypatch)

    settings_mod.store_gemini_key(_FAKE_KEY)
    config = home / "config.toml"
    # Append a hypothetical future section with one string field.
    config.write_text(
        config.read_text(encoding="utf-8")
        + '\n[future_section]\nendpoint = "https://example.test/api"\n',
        encoding="utf-8",
    )

    cleared = settings_mod.clear_gemini_key()

    assert cleared is True
    assert config.exists(), "file with other sections must remain on disk"
    surviving = config.read_text(encoding="utf-8")
    assert _FAKE_KEY not in surviving
    assert "[future_section]" in surviving
    assert "https://example.test/api" in surviving
    # Mode preserved.
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_file_clear_handles_corrupt_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _redirect_config(tmp_path, monkeypatch)
    home.mkdir(parents=True, exist_ok=True)
    config = home / "config.toml"
    config.write_text("this is not = [valid toml at all", encoding="utf-8")

    cleared = settings_mod.clear_gemini_key()

    # Corrupt files get unlinked — better than leaving an unreadable
    # file on disk that might still contain a raw key.
    assert cleared is True
    assert not config.exists()


def test_file_set_preserves_existing_sections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _redirect_config(tmp_path, monkeypatch)
    _disable_keyring(monkeypatch)

    home.mkdir(parents=True, exist_ok=True)
    config = home / "config.toml"
    config.write_text(
        '[future_section]\nendpoint = "https://example.test/api"\n',
        encoding="utf-8",
    )
    os.chmod(config, 0o600)
    os.chmod(home, 0o700)

    settings_mod.store_gemini_key(_FAKE_KEY)

    body = config.read_text(encoding="utf-8")
    import tomllib

    parsed = tomllib.loads(body)
    assert parsed["gemini"]["api_key"] == _FAKE_KEY
    assert parsed["future_section"]["endpoint"] == "https://example.test/api"
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_cli_settings_set_via_env_does_not_print_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _redirect_config(tmp_path, monkeypatch)
    _install_fake_keyring(monkeypatch)
    monkeypatch.setenv("AGENT_DOCTOR_GEMINI_API_KEY_TEST", _FAKE_KEY)

    from agent_doctor import cli

    rc = cli.main([
        "settings",
        "set-gemini-key",
        "--from-env",
        "AGENT_DOCTOR_GEMINI_API_KEY_TEST",
    ])

    captured = capsys.readouterr()
    assert rc == 0
    assert _FAKE_KEY not in captured.out
    assert _FAKE_KEY not in captured.err
    assert settings_mod.load_gemini_key() == _FAKE_KEY


def test_cli_settings_set_from_env_missing_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _redirect_config(tmp_path, monkeypatch)
    _install_fake_keyring(monkeypatch)
    monkeypatch.delenv("AGENT_DOCTOR_GEMINI_API_KEY_MISSING", raising=False)

    from agent_doctor import cli

    rc = cli.main([
        "settings",
        "set-gemini-key",
        "--from-env",
        "AGENT_DOCTOR_GEMINI_API_KEY_MISSING",
    ])

    captured = capsys.readouterr()
    assert rc != 0
    assert "not set" in captured.err.lower() or "empty" in captured.err.lower()


def test_cli_settings_show_does_not_leak_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _redirect_config(tmp_path, monkeypatch)
    _install_fake_keyring(monkeypatch)
    settings_mod.store_gemini_key(_FAKE_KEY)

    from agent_doctor import cli

    rc = cli.main(["settings", "show"])

    captured = capsys.readouterr()
    assert rc == 0
    combined = captured.out + captured.err
    assert _FAKE_KEY not in combined
    assert "configured" in combined
    assert "keyring" in combined or "file" in combined


def test_cli_settings_clear_removes_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _redirect_config(tmp_path, monkeypatch)
    _install_fake_keyring(monkeypatch)
    settings_mod.store_gemini_key(_FAKE_KEY)

    from agent_doctor import cli

    rc = cli.main(["settings", "clear-gemini-key"])

    captured = capsys.readouterr()
    assert rc == 0
    assert _FAKE_KEY not in captured.out
    assert _FAKE_KEY not in captured.err
    assert settings_mod.load_gemini_key() is None
