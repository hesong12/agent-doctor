"""Shared base class for Preferences tab controllers.

Tabs split UI from logic explicitly so the logic layer can be unit-tested
without tkinter. The base class is intentionally tiny — tabs override
``from_settings()`` and ``apply()``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class TabController(ABC):
    @classmethod
    @abstractmethod
    def from_settings(cls) -> "TabController":
        """Load the current persisted settings into a fresh controller."""

    @abstractmethod
    def apply(self) -> None:
        """Persist the controller's current state back to settings."""
