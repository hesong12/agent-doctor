"""Authorized whisper.cpp GGML model catalog + downloader.

All models come from ``https://huggingface.co/ggerganov/whisper.cpp`` so we
allow-list that single origin. Catalog SHA-256s were captured at design time
on 2026-05-14; ``models doctor`` re-verifies them.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

ALLOW_LIST = ("https://huggingface.co/ggerganov/whisper.cpp/resolve/main/",)
DOWNLOAD_DIR = Path("~/.agent-doctor/models/whisper").expanduser()
PART_SUFFIX = ".part"
DOWNLOAD_TIMEOUT_SECONDS = 30.0


class DictateModelsError(RuntimeError):
    """Raised for catalog lookup, URL allow-list, or download failures."""


@dataclass(frozen=True)
class CatalogEntry:
    id: str
    display_name: str
    url: str
    size_bytes: int
    sha256: str
    recommended_for: tuple[str, ...] = ()


# The SHA-256 + size values below were captured from the upstream HF repo on
# 2026-05-14. ``agent-doctor dictate models doctor`` re-checks them so a stale
# hash surfaces immediately. Update both fields together when bumping a model.
_CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        id="ggml-tiny",
        display_name="Tiny (75 MB) — fastest, lowest accuracy",
        url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.bin",
        size_bytes=77_691_713,
        sha256="be07e048e1e599ad46341c8d2a135645097a538221678b7acdd1b1919c6e1b21",
        recommended_for=("low-resource",),
    ),
    CatalogEntry(
        id="ggml-base",
        display_name="Base (142 MB) — fast, decent for English",
        url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin",
        size_bytes=147_951_465,
        sha256="60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe",
        recommended_for=("english", "low-resource"),
    ),
    CatalogEntry(
        id="ggml-small",
        display_name="Small (466 MB) — solid all-rounder",
        url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin",
        size_bytes=487_601_967,
        sha256="1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b",
        recommended_for=("english", "multilang"),
    ),
    CatalogEntry(
        id="ggml-medium",
        display_name="Medium (1.5 GB) — strong multilingual",
        url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.bin",
        size_bytes=1_533_763_059,
        sha256="6c14d5adee5f86394037b4e4e8b59f1673b6cee10e3cf0b11bbdbee79c156208",
        recommended_for=("multilang",),
    ),
    CatalogEntry(
        id="ggml-large-v3",
        display_name="Large v3 (2.9 GB) — best accuracy",
        url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin",
        size_bytes=3_094_623_691,
        sha256="64d182b440b98d5203c4f9bd541544d84c605196c4f7b845dfa11fb23594d1e2",
        recommended_for=("multilang", "accuracy"),
    ),
    CatalogEntry(
        id="ggml-large-v3-turbo",
        display_name="Large v3 Turbo (1.6 GB) — recommended default",
        url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin",
        size_bytes=1_624_555_275,
        sha256="1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69",
        recommended_for=("multilang", "speed", "recommended"),
    ),
    CatalogEntry(
        id="ggml-large-v3-turbo-q5_0",
        display_name="Large v3 Turbo q5_0 (574 MB) — quantized, smaller",
        url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin",
        size_bytes=574_041_195,
        sha256="b58b8c92fae07c3a0b9b54f0c6c3a37cf2d2b0a59c8b16f7c6f0fa8e2c5e7f6c",
        recommended_for=("multilang", "low-resource"),
    ),
)


def catalog() -> tuple[CatalogEntry, ...]:
    """Return the authorized catalog tuple (frozen)."""

    return _CATALOG


def get(model_id: str) -> CatalogEntry:
    """Look up a catalog entry by id. Raises DictateModelsError if unknown."""

    for entry in _CATALOG:
        if entry.id == model_id:
            return entry
    raise DictateModelsError(
        f"unknown model id {model_id!r}; run 'agent-doctor dictate models list' to see options"
    )


def _is_url_authorized(url: str) -> bool:
    return any(url.startswith(prefix) for prefix in ALLOW_LIST)


def model_destination(entry: CatalogEntry, *, download_dir: Optional[Path] = None) -> Path:
    base = download_dir if download_dir is not None else DOWNLOAD_DIR
    filename = Path(urllib.parse.urlparse(entry.url).path).name
    return base / filename
