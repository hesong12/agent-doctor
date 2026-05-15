"""Tests for the LLM provider catalog and probe helpers."""

from __future__ import annotations

import http.server
import json
import socket
import threading
from pathlib import Path
from typing import Iterator

import pytest

from agent_doctor import dictate_llm as dl


def test_catalog_has_three_known_providers() -> None:
    providers = dl.providers()
    ids = {p.id for p in providers}
    assert ids == {"lm_studio", "ollama", "custom"}


def test_get_returns_provider_by_id() -> None:
    p = dl.get_provider("lm_studio")
    assert p.id == "lm_studio"
    assert p.base_url == "http://localhost:1234/v1"
    assert p.models_endpoint == "/models"
    assert p.requires_api_key is False
    assert p.allow_base_url_edit is False


def test_get_unknown_provider_raises() -> None:
    with pytest.raises(dl.DictateLLMError, match="unknown provider"):
        dl.get_provider("nope")


def test_custom_provider_allows_base_url_edit() -> None:
    assert dl.get_provider("custom").allow_base_url_edit is True
