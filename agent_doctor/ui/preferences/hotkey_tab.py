"""Hotkey tab logic (chord capture, PTT/toggle, daemon kickstart)."""

from __future__ import annotations

from dataclasses import dataclass

from agent_doctor import dictate_settings as ds
from agent_doctor import hotkey_install as hi
from agent_doctor import hotkey_parse as hp
from agent_doctor.ui.preferences import permission_probe as pp


# Set once a migration attempt has failed. Prevents the 1Hz snapshot poll
# from repeatedly invoking swiftc when the helper source is broken. Reset
# only by an explicit user toggle via the UI (handled by
# ``hotkey_tab_view._on_daemon_toggle`` on the success path).
_migration_failed: bool = False


def reset_migration_failure_flag() -> None:
    """Allow the next migration attempt to run again.

    Called by the UI when the user explicitly toggles the daemon — the
    user is taking deliberate action, so any prior swiftc failure should
    no longer suppress the migration retry.
    """

    global _migration_failed
    _migration_failed = False


class HotkeyStateError(ValueError):
    pass


@dataclass
class HotkeyState:
    binding: str
    push_to_talk: bool

    @classmethod
    def from_settings(cls) -> "HotkeyState":
        s = ds.load()
        return cls(binding=s.hotkey.binding, push_to_talk=s.hotkey.push_to_talk)

    def apply(self) -> None:
        try:
            chord = hp.parse(self.binding)
        except hp.HotkeyParseError as exc:
            raise HotkeyStateError(str(exc)) from exc
        ptt = bool(self.push_to_talk) or hp.is_modifier_only(chord)
        s = ds.load()
        new = ds.HotkeySettings(
            binding=chord.canonical(),
            push_to_talk=ptt,
            daemon_enabled=s.hotkey.daemon_enabled,
        )
        ds.save(ds.replace_section(s, hotkey=new))
        if hi.DEFAULT_PLIST_PATH.exists():
            hi.sighup()


def install_daemon() -> dict[str, str]:
    return hi.install()


def uninstall_daemon() -> dict[str, str]:
    return hi.uninstall()


def daemon_status() -> dict[str, object]:
    return hi.status()


def daemon_status_snapshot() -> dict[str, object]:
    """Return a snapshot used by the tab view to render the status pill.

    Keys: ``pill`` (one of "listening" / "permission_needed" / "paused" /
    "daemon_stopped"), ``perms`` (PermissionStatus), ``daemon`` (raw dict
    from ``hotkey_install.status()``), ``settings`` (HotkeySettings).

    Migration: if the LaunchAgent is running and the plist exists, treat
    daemon_enabled as True (and persist the correction) even if the user's
    dictate.json still has the old default False — happens on upgrade
    from the pre-Handy-UX branch.

    Defensive: if ``hi.status()`` raises (e.g. ``launchctl`` not on PATH on
    non-macOS or sandboxed environments), report a ``daemon_stopped``
    snapshot so the rest of Preferences stays usable.
    """

    try:
        daemon = hi.status()
    except (FileNotFoundError, OSError):
        # launchctl not available — treat the daemon as stopped. Only the
        # hotkey toggle path is non-functional; other tabs render normally.
        synthetic_daemon: dict[str, object] = {
            "plist_exists": False,
            "helper_exists": False,
            "running": False,
            "plist": "",
            "helper": "",
        }
        perms = pp.PermissionStatus(
            accessibility=False, input_monitoring=False, first_missing=None
        )
        s = ds.load()
        return {
            "pill": "daemon_stopped",
            "perms": perms,
            "daemon": synthetic_daemon,
            "settings": s.hotkey,
        }
    s = ds.load()
    if not daemon["plist_exists"]:
        pill = "daemon_stopped"
        # No probe has run yet — suppress the permission banner.
        perms = pp.PermissionStatus(accessibility=False, input_monitoring=False, first_missing=None)
        return {"pill": pill, "perms": perms, "daemon": daemon, "settings": s.hotkey}

    # Migration: if the LaunchAgent appears loaded but our stored flag
    # is False AND the on-disk helper binary may be from a previous
    # release, rebuild + relaunch the helper so the new binding semantics
    # take effect. Otherwise the UI would show "Listening" with an old
    # binary that doesn't understand right_cmd / modifier-only bindings.
    if daemon["running"] and not s.hotkey.daemon_enabled:
        global _migration_failed
        if not _migration_failed:
            # Preserve any power-user custom --agent-doctor-bin from the
            # existing plist so migration doesn't silently overwrite it
            # with the default.
            existing_bin = hi.read_agent_doctor_bin()
            try:
                hi.install(agent_doctor_bin=existing_bin)  # rebuild + rewrite plist + re-bootstrap
            except hi.HotkeyInstallError:
                # If migration fails (e.g. swiftc missing / source broken),
                # latch the failure so the 1Hz poll doesn't keep retrying
                # an expensive swiftc invocation. The user can clear this
                # by explicitly toggling the daemon via the UI.
                _migration_failed = True
            else:
                new = ds.HotkeySettings(
                    binding=s.hotkey.binding,
                    push_to_talk=s.hotkey.push_to_talk,
                    daemon_enabled=True,
                )
                s = ds.replace_section(s, hotkey=new)
                ds.save(s)
                # Refresh daemon status after restart.
                daemon = hi.status()

    if not daemon["running"] or not s.hotkey.daemon_enabled:
        pill = "paused"
        perms = pp.PermissionStatus(accessibility=True, input_monitoring=True, first_missing=None)
    else:
        perms = pp.check_macos_permissions()
        pill = "listening" if perms.first_missing is None else "permission_needed"
    return {"pill": pill, "perms": perms, "daemon": daemon, "settings": s.hotkey}


def permission_status_snapshot() -> pp.PermissionStatus:
    return pp.check_macos_permissions()
