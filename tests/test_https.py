"""Headless tests for the project's HTTPS CA bundle helper."""

from __future__ import annotations

import builtins
import ssl
import sys

import pytest

from agent_doctor import _https


def test_resolve_ca_bundle_prefers_certifi(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``certifi`` is importable, its path wins over OS candidates."""

    fake_certifi = type(sys)("certifi")
    fake_certifi.where = lambda: "/tmp/fake-certifi.pem"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "certifi", fake_certifi)
    assert _https.resolve_ca_bundle() == "/tmp/fake-certifi.pem"


def test_resolve_ca_bundle_falls_back_to_first_existing_candidate(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``certifi`` is missing, the first existing candidate path wins."""

    # Force certifi import to fail even if it's installed in the runner env.
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "certifi":
            raise ImportError("no certifi for this test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.delitem(sys.modules, "certifi", raising=False)

    fake_bundle = tmp_path / "ca.pem"
    fake_bundle.write_text("dummy", encoding="utf-8")
    monkeypatch.setattr(_https, "_CA_CANDIDATES", (str(fake_bundle),))
    assert _https.resolve_ca_bundle() == str(fake_bundle)


def test_resolve_ca_bundle_returns_none_when_nothing_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "certifi":
            raise ImportError("no certifi for this test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.delitem(sys.modules, "certifi", raising=False)
    monkeypatch.setattr(_https, "_CA_CANDIDATES", ("/nonexistent/path/ca.pem",))
    assert _https.resolve_ca_bundle() is None


def test_make_https_context_returns_ssl_context() -> None:
    ctx = _https.make_https_context()
    assert isinstance(ctx, ssl.SSLContext)
