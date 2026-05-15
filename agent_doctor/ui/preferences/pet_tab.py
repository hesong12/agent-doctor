"""Pet tab logic (animation toggles + sprite picker bridge)."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from agent_doctor import dictate_settings as ds


class PetUiStateError(ValueError):
    pass


@dataclass
class PetUiState:
    animate_listening: bool
    animate_thinking: bool

    @classmethod
    def from_settings(cls) -> "PetUiState":
        s = ds.load()
        return cls(
            animate_listening=s.pet.animate_listening,
            animate_thinking=s.pet.animate_thinking,
        )

    def apply(self) -> None:
        s = ds.load()
        new = ds.PetSettings(
            animate_listening=bool(self.animate_listening),
            animate_thinking=bool(self.animate_thinking),
        )
        ds.save(ds.replace_section(s, pet=new))


def set_sprite_path(source: Path) -> Path:
    """Copy ``source`` to the user sprite path; returns the destination."""

    from agent_doctor.pet_display import user_sprite_path

    dest = user_sprite_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        raise PetUiStateError(f"sprite not found: {source}")
    shutil.copyfile(source, dest)
    return dest
