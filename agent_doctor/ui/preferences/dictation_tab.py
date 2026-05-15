"""Dictation tab logic (model, language, buffer)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from agent_doctor import dictate_models as dm
from agent_doctor import dictate_settings as ds


class DictationStateError(ValueError):
    pass


@dataclass
class DictationState:
    model_id: Optional[str]
    model_path: Optional[str]
    language: str
    extra_buffer_ms: int

    @classmethod
    def from_settings(cls) -> "DictationState":
        s = ds.load()
        return cls(
            model_id=s.transcription.model_id,
            model_path=s.transcription.model_path,
            language=s.transcription.language,
            extra_buffer_ms=s.transcription.extra_buffer_ms,
        )

    def apply(self) -> None:
        if self.extra_buffer_ms < 0 or self.extra_buffer_ms > 500:
            raise DictationStateError(
                f"extra_buffer_ms must be 0..500 (got {self.extra_buffer_ms})"
            )
        s = ds.load()
        new = ds.TranscriptionSettings(
            model_id=self.model_id,
            model_path=self.model_path,
            language=self.language,
            extra_buffer_ms=int(self.extra_buffer_ms),
        )
        ds.save(ds.replace_section(s, transcription=new))


def model_install_options() -> List[dict]:
    """Return one row per catalog entry, augmented with install status."""

    return dm.list_status()


def select_model(model_id: str) -> None:
    """Make ``model_id`` the active transcription model. Caller must ensure it
    is installed."""

    dm.set_active(model_id)
