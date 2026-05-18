"""Hotkey tab logic (chord capture, PTT/toggle, daemon kickstart)."""

from __future__ import annotations

from dataclasses import dataclass

from agent_doctor import dictate_settings as ds
from agent_doctor import hotkey_install as hi
from agent_doctor import hotkey_parse as hp


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
