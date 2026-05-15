"""Tests for the whisper.cpp model catalog + downloader."""

from __future__ import annotations

import hashlib
import http.server
import json
import socket
import threading
from pathlib import Path
from typing import Iterator

import pytest

from agent_doctor import dictate_models as dm


def test_catalog_is_non_empty_and_well_formed() -> None:
    catalog = dm.catalog()
    assert len(catalog) >= 4
    ids = {entry.id for entry in catalog}
    # Sanity: the Handy-compatible default must be present.
    assert "ggml-large-v3-turbo" in ids
    for entry in catalog:
        assert entry.url.startswith(
            "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
        )
        assert entry.size_bytes > 0
        assert len(entry.sha256) == 64
        assert entry.display_name
        assert isinstance(entry.recommended_for, tuple)


def test_get_returns_catalog_entry() -> None:
    entry = dm.get("ggml-large-v3-turbo")
    assert entry.id == "ggml-large-v3-turbo"


def test_get_unknown_raises() -> None:
    with pytest.raises(dm.DictateModelsError, match="unknown"):
        dm.get("does-not-exist")
