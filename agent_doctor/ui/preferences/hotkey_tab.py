"""Hotkey tab logic (chord capture, PTT/toggle, daemon kickstart)."""

from __future__ import annotations

from dataclasses import dataclass

from agent_doctor import dictate_settings as ds
from agent_doctor import hotkey_install as hi
from agent_doctor import hotkey_parse as hp
from agent_doctor.ui.preferences import permission_probe as pp


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
    """

    daemon = hi.status()
    s = ds.load()
    if not daemon["plist_exists"]:
        pill = "daemon_stopped"
        # No probe has run yet — suppress the permission banner.
        perms = pp.PermissionStatus(accessibility=False, input_monitoring=False, first_missing=None)
        return {"pill": pill, "perms": perms, "daemon": daemon, "settings": s.hotkey}

    # Migration: backfill daemon_enabled when LaunchAgent is actually running.
    if daemon["running"] and not s.hotkey.daemon_enabled:
        new = ds.HotkeySettings(
            binding=s.hotkey.binding,
            push_to_talk=s.hotkey.push_to_talk,
            daemon_enabled=True,
        )
        s = ds.replace_section(s, hotkey=new)
        ds.save(s)

    if not daemon["running"] or not s.hotkey.daemon_enabled:
        pill = "paused"
        perms = pp.PermissionStatus(accessibility=True, input_monitoring=True, first_missing=None)
    else:
        perms = pp.check_macos_permissions()
        pill = "listening" if perms.first_missing is None else "permission_needed"
    return {"pill": pill, "perms": perms, "daemon": daemon, "settings": s.hotkey}


def permission_status_snapshot() -> pp.PermissionStatus:
    return pp.check_macos_permissions()
