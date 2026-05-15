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


@pytest.fixture
def fake_hf_server(tmp_path: Path) -> Iterator[tuple[str, dict[str, bytes]]]:
    """Spin a local HTTP server that maps paths to canned bytes."""

    payloads: dict[str, bytes] = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = payloads.get(self.path)
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = http.server.HTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", payloads
    finally:
        server.shutdown()
        thread.join(timeout=2.0)


def test_download_writes_file_and_verifies_hash(
    tmp_path: Path, fake_hf_server: tuple[str, dict[str, bytes]]
) -> None:
    base_url, payloads = fake_hf_server
    body = b"ggml-tiny-bytes" * 1024
    digest = hashlib.sha256(body).hexdigest()
    entry = dm.CatalogEntry(
        id="ggml-tiny",
        display_name="Tiny",
        url=f"{base_url}/ggml-tiny.bin",
        size_bytes=len(body),
        sha256=digest,
    )
    payloads["/ggml-tiny.bin"] = body

    dest = tmp_path / "ggml-tiny.bin"
    progress_calls: list[tuple[int, int]] = []

    dm._download_one(
        entry,
        dest,
        allow_list=(f"{base_url}/",),
        progress=lambda done, total: progress_calls.append((done, total)),
    )

    assert dest.read_bytes() == body
    assert progress_calls[-1] == (len(body), len(body))
    assert not dest.with_suffix(dest.suffix + dm.PART_SUFFIX).exists()


def test_download_rejects_unauthorized_url(tmp_path: Path) -> None:
    entry = dm.CatalogEntry(
        id="evil",
        display_name="evil",
        url="https://evil.example/whisper.bin",
        size_bytes=1,
        sha256="0" * 64,
    )
    with pytest.raises(dm.DictateModelsError, match="not in the allow-list"):
        dm._download_one(entry, tmp_path / "x.bin", allow_list=dm.ALLOW_LIST)


def test_download_sha_mismatch_deletes_partial(
    tmp_path: Path, fake_hf_server: tuple[str, dict[str, bytes]]
) -> None:
    base_url, payloads = fake_hf_server
    body = b"actual bytes"
    payloads["/x.bin"] = body
    entry = dm.CatalogEntry(
        id="x",
        display_name="x",
        url=f"{base_url}/x.bin",
        size_bytes=len(body),
        sha256="0" * 64,
    )
    dest = tmp_path / "x.bin"
    with pytest.raises(dm.DictateModelsError, match="sha256 mismatch"):
        dm._download_one(entry, dest, allow_list=(f"{base_url}/",))
    assert not dest.exists()
    assert not dest.with_suffix(dest.suffix + dm.PART_SUFFIX).exists()


def test_set_persists_model_in_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_doctor import dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    monkeypatch.setattr(dm, "DOWNLOAD_DIR", tmp_path / "models")

    # Fake an already-installed file with matching hash.
    entry = dm.get("ggml-tiny")
    body = b"x" * 64
    digest = hashlib.sha256(body).hexdigest()
    monkeypatch.setattr(
        dm,
        "_CATALOG",
        tuple(
            dm.CatalogEntry(
                id=e.id,
                display_name=e.display_name,
                url=e.url,
                size_bytes=len(body),
                sha256=digest,
                recommended_for=e.recommended_for,
            )
            if e.id == "ggml-tiny"
            else e
            for e in dm._CATALOG
        ),
    )
    dest = dm.model_destination(dm.get("ggml-tiny"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)

    dm.set_active("ggml-tiny")
    settings = ds.load()
    assert settings.transcription.model_id == "ggml-tiny"
    assert settings.transcription.model_path == str(dest)


def test_set_refuses_when_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_doctor import dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    monkeypatch.setattr(dm, "DOWNLOAD_DIR", tmp_path / "models")

    with pytest.raises(dm.DictateModelsError, match="not installed"):
        dm.set_active("ggml-tiny")


def test_remove_deletes_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dm, "DOWNLOAD_DIR", tmp_path / "models")
    entry = dm.get("ggml-tiny")
    dest = dm.model_destination(entry)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"x")
    dm.remove("ggml-tiny")
    assert not dest.exists()


def test_installed_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dm, "DOWNLOAD_DIR", tmp_path / "models")
    entry = dm.get("ggml-tiny")
    assert dm.installed_path(entry) is None
    dest = dm.model_destination(entry)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"y")
    assert dm.installed_path(entry) == dest
