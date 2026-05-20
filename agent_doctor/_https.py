"""Robust HTTPS SSL context for the project's urllib.request callers.

Python on macOS (Python.org installer, uv-managed venvs) ships without a
default CA bundle — ``ssl.get_default_verify_paths()`` returns
``cafile=None, capath=None`` until the user runs the
``Install Certificates.command`` shim. Connections to anything HTTPS then
fail with ``CERTIFICATE_VERIFY_FAILED``.

This module sidesteps the install step by probing for a CA bundle on disk
in the platforms we care about: certifi (if any optional dep pulled it in),
the macOS system bundle, Homebrew's openssl bundle, and the common Linux
distro paths. If none is found, we hand back a default context (which
upstream callers will still try, and which will produce the same error the
user would see today — no regression).

Used by both :mod:`agent_doctor.dictate_llm` (probe ``/models``) and
:mod:`agent_doctor.dictate` (live LLM call) so that the Gemini
OpenAI-compatible endpoint and any future HTTPS provider both work without
requiring the user to fix Python's SSL setup manually.
"""

from __future__ import annotations

import os
import ssl
from typing import Optional

_CA_CANDIDATES: tuple[str, ...] = (
    "/etc/ssl/cert.pem",  # macOS system bundle
    "/opt/homebrew/etc/ca-certificates/cert.pem",  # Homebrew arm64
    "/usr/local/etc/ca-certificates/cert.pem",  # Homebrew intel
    "/etc/pki/tls/certs/ca-bundle.crt",  # RHEL/Fedora/CentOS
    "/etc/ssl/certs/ca-certificates.crt",  # Debian/Ubuntu
)


def resolve_ca_bundle() -> Optional[str]:
    """Return a path to a CA bundle on disk, or ``None`` to use Python defaults.

    Order of preference: ``certifi`` (if importable) → system bundle paths
    in :data:`_CA_CANDIDATES`. Returns ``None`` when nothing is found so the
    caller can fall through to :func:`ssl.create_default_context` with no
    explicit cafile (and produce the same SSL error the user would see
    today, without us silently disabling verification).
    """

    try:
        import certifi  # type: ignore[import-not-found]
        return certifi.where()
    except ImportError:
        pass
    for path in _CA_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


def make_https_context() -> ssl.SSLContext:
    """Build an ``SSLContext`` using the best available CA bundle."""

    cafile = resolve_ca_bundle()
    return ssl.create_default_context(cafile=cafile)
