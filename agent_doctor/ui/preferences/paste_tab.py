"""Paste tab logic (auto-paste toggle, paste delay, permission test)."""

from __future__ import annotations

from dataclasses import dataclass

from agent_doctor import dictate_paste as dp
from agent_doctor import dictate_settings as ds


class PasteStateError(ValueError):
    pass


@dataclass
class PasteState:
    auto_paste: bool
    paste_delay_ms: int

    @classmethod
    def from_settings(cls) -> "PasteState":
        s = ds.load()
        return cls(auto_paste=s.paste.auto_paste, paste_delay_ms=s.paste.paste_delay_ms)

    def apply(self) -> None:
        if self.paste_delay_ms < 0 or self.paste_delay_ms > 250:
            raise PasteStateError(
                f"paste_delay_ms must be 0..250 (got {self.paste_delay_ms})"
            )
        if self.auto_paste:
            try:
                dp.enable()
            except dp.PasteError as exc:
                raise PasteStateError(str(exc)) from exc
        else:
            dp.disable()
        s = ds.load()
        new = ds.PasteSettings(
            auto_paste=self.auto_paste,
            paste_delay_ms=int(self.paste_delay_ms),
            last_permission_check=s.paste.last_permission_check,
        )
        ds.save(ds.replace_section(s, paste=new))


def permission_test() -> bool:
    return dp.permission_test()
