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


@pytest.fixture
def fake_openai_server() -> Iterator[tuple[str, dict[str, object]]]:
    payloads: dict[str, object] = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/v1/models":
                body = payloads.get("body", {"data": []})
                status = int(payloads.get("status", 200))
                if isinstance(body, (bytes, str)):
                    raw = body.encode("utf-8") if isinstance(body, str) else body
                else:
                    raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            self.send_response(404)
            self.end_headers()

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
        yield f"http://127.0.0.1:{port}/v1", payloads
    finally:
        server.shutdown()
        thread.join(timeout=2.0)


def test_probe_reachable_returns_model_ids(
    fake_openai_server: tuple[str, dict[str, object]],
) -> None:
    base_url, payloads = fake_openai_server
    payloads["body"] = {
        "object": "list",
        "data": [
            {"id": "qwen2.5-7b-instruct", "object": "model"},
            {"id": "llama-3.1-8b", "object": "model"},
        ],
    }

    result = dl.probe(base_url, "/models")
    assert result.reachable is True
    assert result.models == ["qwen2.5-7b-instruct", "llama-3.1-8b"]
    assert result.error is None


def test_probe_404_returns_unreachable_with_reason(
    fake_openai_server: tuple[str, dict[str, object]],
) -> None:
    base_url, payloads = fake_openai_server
    payloads["status"] = 404
    payloads["body"] = "nope"
    result = dl.probe(base_url, "/models")
    assert result.reachable is False
    assert "404" in (result.error or "")


def test_probe_unreachable_host_does_not_raise() -> None:
    result = dl.probe("http://127.0.0.1:1", "/models", timeout=1.0)
    assert result.reachable is False
    assert result.error is not None
    assert result.models == []


def test_probe_all_returns_one_row_per_provider(
    fake_openai_server: tuple[str, dict[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    base_url, payloads = fake_openai_server
    payloads["body"] = {"data": [{"id": "stub"}]}

    # Override every catalog entry to point at the fake server so the test
    # never hits localhost ports for real.
    fake_providers = tuple(
        dl.Provider(
            id=p.id,
            label=p.label,
            base_url=base_url,
            models_endpoint=p.models_endpoint,
            requires_api_key=p.requires_api_key,
            allow_base_url_edit=p.allow_base_url_edit,
        )
        for p in dl.providers()
    )
    monkeypatch.setattr(dl, "_PROVIDERS", fake_providers)
    rows = dl.probe_all(timeout=1.0)
    assert {r.provider_id for r in rows} == {"lm_studio", "ollama", "custom"}
    for row in rows:
        assert row.reachable is True
        assert row.models == ["stub"]
