"""Build the Swift hotkey helper and register it with launchd.

Public API:
- ``build(src, dest)`` — compile the Swift source.
- ``write_plist(path, helper, agent_doctor_bin)`` — produce the LaunchAgent plist.
- ``install(agent_doctor_bin=None)`` — build, write plist, launchctl bootstrap.
- ``sighup()`` — kick the running daemon so it re-reads ``dictate.json``.
- ``pause()`` — stop the running LaunchAgent without removing the plist.
- ``resume()`` — re-bootstrap an existing plist (no rebuild).
- ``uninstall()`` — launchctl bootout + remove the plist.

All shell-outs go through ``_run_launchctl`` so tests can stub them.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

LABEL = "com.agent-doctor.hotkey"
DEFAULT_HELPER_PATH = Path(
    "~/Library/Application Support/agent-doctor/bin/agent-doctor-hotkey"
).expanduser()
DEFAULT_PLIST_PATH = Path(f"~/Library/LaunchAgents/{LABEL}.plist").expanduser()
SWIFT_SOURCE = Path(__file__).with_name("hotkey") / "HotkeyHelper.swift"

# Stable identifier the Swift helper is signed with. macOS TCC tracks
# signed binaries by (cert, identifier) rather than cdhash, so as long as
# the user's keychain has the same signing cert and we always pass this
# identifier to codesign, an agent-doctor upgrade can ship a new Swift
# source — and produce a new cdhash — without invalidating the user's
# Input Monitoring grant.
HELPER_BUNDLE_ID = "com.agent-doctor.hotkey"

# Common Name of the self-signed code-signing cert created in the user's
# login keychain. Matches the keychain identity passed to ``codesign
# -s``. Stable for the lifetime of the keychain; not tied to Apple
# Developer ID so works on machines without an Apple Developer account.
SIGNING_IDENTITY = "agent-doctor-hotkey-signing"


class HotkeyInstallError(RuntimeError):
    """Raised when the hotkey helper cannot be built or registered."""


def build(src: Path, dest: Path) -> Path:
    """Compile ``src`` with ``swiftc`` into ``dest`` (chmod 0755)."""

    swiftc = shutil.which("swiftc")
    if swiftc is None:
        raise HotkeyInstallError(
            "swiftc not found on PATH; install Xcode Command Line Tools with "
            "'xcode-select --install'"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Augment PATH for the child so shebangs like ``#!/usr/bin/env bash``
    # in build wrappers can still resolve when callers set a sparse PATH.
    child_env = dict(os.environ)
    child_env["PATH"] = (
        child_env.get("PATH", "")
        + os.pathsep
        + "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
    )
    proc = subprocess.run(
        [swiftc, "-O", str(src), "-o", str(dest)],
        capture_output=True,
        check=False,
        env=child_env,
    )
    if proc.returncode != 0:
        raise HotkeyInstallError(
            f"swiftc failed (rc={proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace')}"
        )
    dest.chmod(0o755)
    return dest


def _find_openssl() -> str:
    """Locate an openssl binary suitable for PKCS12 export.

    Prefers Homebrew's OpenSSL 3 over macOS' bundled LibreSSL because
    Homebrew tracks upstream more closely and accepts the same flags.
    Both work for the explicit PBE-SHA1-3DES path we use, so falling
    back to LibreSSL is fine.
    """

    for candidate in (
        "/opt/homebrew/bin/openssl",  # Apple Silicon Homebrew
        "/usr/local/bin/openssl",     # Intel Homebrew
    ):
        if Path(candidate).exists():
            return candidate
    found = shutil.which("openssl")
    if found:
        return found
    raise HotkeyInstallError(
        "openssl not found on PATH; required to generate the helper "
        "signing certificate. Install with `brew install openssl`."
    )


def _login_keychain_path() -> Optional[Path]:
    """Return the path to the user's login keychain, or None if unknown.

    Queries ``security login-keychain`` rather than hardcoding
    ``~/Library/Keychains/login.keychain-db`` because the path is not
    guaranteed: legacy installs may still use the older ``.keychain``
    extension, and FileVault-migrated accounts can put the keychain in
    a non-default location. ``security`` always knows the truth.
    """

    sec = shutil.which("security")
    if sec is None:
        return None
    try:
        proc = subprocess.run(
            [sec, "login-keychain"],
            capture_output=True,
            check=False,
        )
    except OSError:
        # FileNotFoundError is a subclass of OSError, so OSError alone
        # covers both the "binary disappeared between which() and run()"
        # race and generic exec failures.
        return None
    if proc.returncode != 0:
        return None
    # security prints the path quoted on its own line; strip whitespace
    # and surrounding double quotes before turning into a Path.
    line = proc.stdout.decode("utf-8", "replace").strip().splitlines()
    if not line:
        return None
    raw = line[0].strip().strip('"').strip()
    if not raw:
        return None
    return Path(raw)


def _signing_identity_exists(identity: str = SIGNING_IDENTITY) -> bool:
    """Return True if a code-signing certificate matching ``identity`` is
    already present in the user's keychain.

    Uses ``security find-certificate -c <CN>`` rather than the more
    common ``find-identity -p codesigning`` because the latter filters
    by trust-settings policy and self-signed certs without explicit
    "trust for code signing" entries don't appear there. ``codesign``
    itself still uses the cert successfully because the private key is
    in the keychain and ACL-allowed for codesign — so detection has to
    match what codesign can use, not what the policy filter shows.
    """

    try:
        result = subprocess.run(
            ["security", "find-certificate", "-c", identity],
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0


def ensure_signing_identity(identity: str = SIGNING_IDENTITY) -> None:
    """Create a self-signed code-signing cert in the user's login keychain
    if one matching ``identity`` does not already exist.

    The cert has ``extendedKeyUsage = codeSigning`` and a 100-year expiry
    so the helper signature does not silently rot under the user. The
    private key is imported with ``-T /usr/bin/codesign`` so codesign
    can use it without re-prompting after the first "Always Allow"
    keychain dialog. Idempotent — re-entry on an existing cert is a
    cheap ``find-identity`` lookup that returns immediately.
    """

    if _signing_identity_exists(identity):
        return
    openssl = _find_openssl()
    sec = shutil.which("security")
    if sec is None:
        raise HotkeyInstallError(
            "security CLI not found; this command is part of macOS — "
            "agent-doctor's hotkey daemon only runs on Darwin."
        )
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        key_path = tmp_path / "key.pem"
        cert_path = tmp_path / "cert.pem"
        p12_path = tmp_path / "cert.p12"
        config_path = tmp_path / "openssl.cnf"
        config_path.write_text(
            "[ req ]\n"
            "distinguished_name = req_dn\n"
            "prompt = no\n"
            "x509_extensions = v3_codesign\n"
            "\n"
            "[ req_dn ]\n"
            f"CN = {identity}\n"
            "\n"
            "[ v3_codesign ]\n"
            "basicConstraints = critical, CA:false\n"
            "keyUsage = critical, digitalSignature\n"
            "extendedKeyUsage = critical, codeSigning\n",
            encoding="utf-8",
        )
        gen = subprocess.run(
            [
                openssl, "req",
                "-newkey", "rsa:2048",
                "-nodes",
                "-keyout", str(key_path),
                "-x509",
                "-days", "36500",
                "-out", str(cert_path),
                "-config", str(config_path),
            ],
            capture_output=True,
            check=False,
        )
        if gen.returncode != 0:
            raise HotkeyInstallError(
                f"openssl req failed (rc={gen.returncode}): "
                f"{gen.stderr.decode('utf-8', 'replace')}"
            )
        # macOS' security tool refuses PKCS12 files with an empty password
        # because the HMAC verification step uses the password as the key;
        # ``pass:`` ends up generating a key that doesn't match the file,
        # so we use a non-empty placeholder consistently on both sides.
        # The placeholder is not a secret — the private key is intended
        # to live only on this machine, only ever used by codesign.
        p12_password = "agent-doctor-keychain"
        # macOS' security tool only supports the older PKCS12 algorithms
        # (PBES1/PBKDF1 + 3DES). OpenSSL 3.x defaults to PBES2/PBKDF2 +
        # AES-256, which security rejects with a (misleading)
        # "MAC verification failed" error. We force the older format
        # via explicit -keypbe / -certpbe / -macalg args instead of
        # OpenSSL 3's -legacy flag, because LibreSSL (which ships as
        # /usr/bin/openssl on stock macOS) doesn't accept -legacy.
        # The explicit-algorithm form works on both LibreSSL and
        # OpenSSL 3.
        pkg = subprocess.run(
            [
                openssl, "pkcs12",
                "-export",
                "-in", str(cert_path),
                "-inkey", str(key_path),
                "-out", str(p12_path),
                "-password", f"pass:{p12_password}",
                "-name", identity,
                "-keypbe", "PBE-SHA1-3DES",
                "-certpbe", "PBE-SHA1-3DES",
                "-macalg", "sha1",
            ],
            capture_output=True,
            check=False,
        )
        if pkg.returncode != 0:
            raise HotkeyInstallError(
                f"openssl pkcs12 failed (rc={pkg.returncode}): "
                f"{pkg.stderr.decode('utf-8', 'replace')}"
            )
        # Build the import argv. Prefer the path that ``security
        # login-keychain`` reports; if that lookup fails (sandboxed env,
        # custom keychain config), omit ``-k`` so ``security import``
        # uses the user's default keychain — which is the login
        # keychain in practice for user-run processes.
        import_argv = [sec, "import", str(p12_path)]
        login_keychain = _login_keychain_path()
        if login_keychain is not None:
            import_argv += ["-k", str(login_keychain)]
        import_argv += [
            "-P", p12_password,
            "-T", "/usr/bin/codesign",
        ]
        imp = subprocess.run(
            import_argv,
            capture_output=True,
            check=False,
        )
        if imp.returncode != 0:
            raise HotkeyInstallError(
                f"security import failed (rc={imp.returncode}): "
                f"{imp.stderr.decode('utf-8', 'replace')}"
            )


def sign_helper(
    helper: Path,
    identity: str = SIGNING_IDENTITY,
    bundle_id: str = HELPER_BUNDLE_ID,
) -> None:
    """Apply a stable code signature to ``helper`` using ``identity``.

    The combination of a stable signing identity and a stable
    ``--identifier`` is what lets macOS TCC carry the user's Input
    Monitoring grant across rebuilds — without it every recompile
    produces a new adhoc signature whose cdhash invalidates the grant.

    No extra ``--options`` flag is needed for the TCC tracking
    behavior: ``library`` is intended for dynamic libraries (enforces
    Library Validation on load), and ``runtime`` is only useful when
    notarizing through Apple. The helper is a self-signed standalone
    executable, so a plain signature is correct and sufficient.
    """

    codesign = shutil.which("codesign")
    if codesign is None:
        raise HotkeyInstallError(
            "codesign not found on PATH; required to sign the hotkey helper. "
            "Install Xcode Command Line Tools with `xcode-select --install`."
        )
    proc = subprocess.run(
        [
            codesign, "--force",
            "-s", identity,
            "--identifier", bundle_id,
            str(helper),
        ],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise HotkeyInstallError(
            f"codesign failed (rc={proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace')}"
        )


def write_plist(path: Path, helper: Path, agent_doctor_bin: str) -> Path:
    """Write a LaunchAgent plist that runs ``helper`` with ``agent_doctor_bin`` in env."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": LABEL,
        "ProgramArguments": [str(helper)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "EnvironmentVariables": {
            "AGENT_DOCTOR_BIN": agent_doctor_bin,
            # macOS does not inherit PATH for launchd-managed processes; bake one.
            "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin",
        },
        "StandardOutPath": str(
            Path("~/Library/Logs/agent-doctor-hotkey.log").expanduser()
        ),
        "StandardErrorPath": str(
            Path("~/Library/Logs/agent-doctor-hotkey.err.log").expanduser()
        ),
    }
    body = plistlib.dumps(payload)
    path.write_bytes(body)
    return path


def _run_launchctl(
    argv: list[str], *, check: bool = False
) -> subprocess.CompletedProcess:
    """Run a launchctl command. Centralised so tests can stub one place."""

    return subprocess.run(argv, capture_output=True, check=check)


def _domain_target() -> str:
    return f"gui/{os.getuid()}"


def _source_fingerprint(src: Path) -> str:
    """SHA-256 hex digest of the Swift source. Content-based so it is
    immune to mtime perturbations from tarball restore, scp -p, package
    extraction, etc. — see codex review feedback that motivated the
    switch from mtime to hash."""

    h = hashlib.sha256()
    with src.open("rb") as fp:
        for chunk in iter(lambda: fp.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _fingerprint_sidecar(helper: Path) -> Path:
    return helper.with_name(helper.name + ".source-sha256")


def _signature_marker(helper: Path) -> Path:
    """Sidecar file recording which signing identity last touched the
    helper. Lets install() detect "already signed with the stable
    agent-doctor identity" without parsing codesign output."""

    return helper.with_name(helper.name + ".signed-by")


def _is_signed_with_stable_identity(helper: Path) -> bool:
    """Return True iff our signature marker records the current stable
    identity. Marker missing or mismatched → caller will sign and
    refresh the marker."""

    try:
        recorded = _signature_marker(helper).read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return recorded == SIGNING_IDENTITY


def _should_rebuild(src: Path, helper: Path) -> bool:
    """Return True if ``helper`` needs to be rebuilt from ``src``.

    Rebuild when ANY of:
    - ``helper`` does not exist (fresh install),
    - ``helper`` is not a regular file (broken/replaced by directory etc.),
    - ``helper`` is missing the user-execute bit (a previous restore
      stripped permissions and only ``build()`` chmods 0o755),
    - the sidecar fingerprint is missing or does not match the current
      source hash (upgrade path — bundled Swift bumped since last build).

    Otherwise the existing helper was compiled from this exact source
    revision, so we leave the file untouched. This is the whole point of
    the function: an unchanged binary keeps the same cdhash, and macOS
    Input Monitoring (TCC) tracks adhoc-signed binaries by cdhash. Any
    rebuild — even producing a byte-identical binary — silently
    invalidates the user's previously-granted Input Monitoring
    permission.
    """

    try:
        helper_stat = helper.stat()
    except FileNotFoundError:
        return True
    except OSError:
        return True
    if not helper.is_file():
        return True
    # 0o100 == owner-execute bit; launchd cannot exec a non-executable file
    # so a helper without it would silently fail at bootstrap time.
    if not helper_stat.st_mode & 0o100:
        return True
    sidecar = _fingerprint_sidecar(helper)
    try:
        recorded = sidecar.read_text(encoding="utf-8").strip()
    except OSError:
        # No sidecar means we don't know what source built this helper.
        # Safer to rebuild than to assume it matches.
        return True
    try:
        current = _source_fingerprint(src)
    except OSError:
        # Source unreadable but helper is executable + has a recorded
        # fingerprint. Prefer reusing what's on disk over failing the
        # install entirely; launchd will surface a real exec error if
        # the helper itself turns out to be corrupt.
        return False
    return recorded != current


def install(*, agent_doctor_bin: Optional[str] = None) -> dict[str, object]:
    """Build the helper if needed, write the plist, and launchctl-bootstrap it.

    Rebuild policy: only recompile when :func:`_should_rebuild` says the
    on-disk helper is stale relative to the Swift source. Skipping the
    rebuild when the binary is already current preserves its cdhash so
    macOS Input Monitoring (TCC) does not silently invalidate the user's
    granted permission on every Background-daemon toggle.

    If ``agent_doctor_bin`` is not provided and an existing plist is on
    disk, reuse the value from that plist — preserves a power user's
    custom ``--agent-doctor-bin`` choice across resume/migration paths.
    Only falls back to ``which agent-doctor`` when there's no existing
    plist (i.e. true fresh install).
    """

    helper = DEFAULT_HELPER_PATH
    plist = DEFAULT_PLIST_PATH
    bin_path = (
        agent_doctor_bin
        or read_agent_doctor_bin()
        or shutil.which("agent-doctor")
        or "/usr/local/bin/agent-doctor"
    )
    rebuilt = False
    signed = False
    if _should_rebuild(SWIFT_SOURCE, helper):
        build(SWIFT_SOURCE, helper)
        # The new binary inherits swiftc's adhoc signature, so any
        # pre-existing marker is stale by definition. Clear it now,
        # BEFORE attempting sign_helper(): if signing later raises
        # (locked keychain, ACL prompt declined, etc.), the absence of
        # the marker forces the next install() to retry the sign step
        # — without this clear, a transient failure would leave the
        # rebuilt-but-unsigned binary stranded with a stale marker
        # claiming it had the stable identity.
        #
        # Deliberately NOT wrapped in try/except: if the marker file
        # cannot be removed (permission-denied, chflags +immutable,
        # filesystem read-only), we must not continue past this point.
        # Suppressing the failure would reintroduce exactly the bug
        # this clear was meant to fix — the original Codex P1 finding
        # against PR #37. Raising here is also safer than the
        # alternative "leave the install half-done with a stale
        # marker": the user sees a clear, actionable error rather
        # than a silent regression of the Input Monitoring guarantee
        # on the next install() run.
        _signature_marker(helper).unlink(missing_ok=True)
        # Record the source fingerprint next to the helper so the next
        # install() call can decide "rebuild or reuse" by content rather
        # than mtime (which restores/copies can perturb).
        try:
            _fingerprint_sidecar(helper).write_text(
                _source_fingerprint(SWIFT_SOURCE), encoding="utf-8"
            )
        except OSError:
            # Sidecar write failure is non-fatal — the next install()
            # will just rebuild again, which is correct behavior.
            pass
        rebuilt = True
    # Signing path:
    # - If we just rebuilt, the helper bytes changed so swiftc's default
    #   adhoc signature is back regardless of the on-disk marker — we
    #   must re-sign to restore the stable identity.
    # - If we did not rebuild but the marker is missing/stale, this is
    #   the upgrade path: existing users with a pre-stable-signing
    #   helper get migrated without first forcing a recompile.
    # - Otherwise the marker is current → signing is idempotent.
    if rebuilt or not _is_signed_with_stable_identity(helper):
        ensure_signing_identity()
        sign_helper(helper)
        try:
            _signature_marker(helper).write_text(SIGNING_IDENTITY, encoding="utf-8")
        except OSError:
            # Marker write failure is non-fatal — the next install() will
            # re-sign, which is wasteful but correct.
            pass
        signed = True
    write_plist(plist, helper, bin_path)
    # Best-effort bootout in case a stale agent is loaded; ignore its rc.
    _run_launchctl(["launchctl", "bootout", f"{_domain_target()}/{LABEL}"])
    proc = _run_launchctl(
        ["launchctl", "bootstrap", _domain_target(), str(plist)]
    )
    if proc.returncode != 0:
        raise HotkeyInstallError(
            f"launchctl bootstrap failed (rc={proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace')}"
        )
    return {
        "helper": str(helper),
        "plist": str(plist),
        "agent_doctor_bin": bin_path,
        "rebuilt": rebuilt,
        "signed": signed,
    }


def sighup() -> bool:
    """Send SIGHUP to the running daemon. Returns True on success."""

    proc = _run_launchctl(
        ["launchctl", "kill", "SIGHUP", f"{_domain_target()}/{LABEL}"]
    )
    return proc.returncode == 0


def pause() -> bool:
    """Stop the running LaunchAgent without removing the plist.

    Returns True iff launchctl bootout reported success. The plist itself is
    untouched so a subsequent :func:`resume` can re-bootstrap without a full
    rebuild.
    """

    proc = _run_launchctl(["launchctl", "bootout", f"{_domain_target()}/{LABEL}"])
    return proc.returncode == 0


def resume(*, agent_doctor_bin: Optional[str] = None) -> dict[str, object]:
    """Delegate to :func:`install`. Kept as a named alias so the UI/CLI can
    distinguish "resume from pause" intent from "fresh install" in their
    messaging.

    The upgrade-safety concern (a pre-Handy-UX helper sitting on disk that
    doesn't understand ``right_cmd``) is now handled inside
    :func:`install` via :func:`_should_rebuild`: when the bundled Swift
    source is newer than the on-disk helper, install rebuilds; when they
    match, install reuses the binary so its cdhash — and the user's
    Input Monitoring grant — survive.
    """

    return install(agent_doctor_bin=agent_doctor_bin)


def uninstall() -> dict[str, str]:
    """Bootout the LaunchAgent and remove the plist."""

    plist = DEFAULT_PLIST_PATH
    _run_launchctl(["launchctl", "bootout", f"{_domain_target()}/{LABEL}"])
    plist.unlink(missing_ok=True)
    return {"plist_removed": str(plist)}


def read_agent_doctor_bin() -> Optional[str]:
    """Read the AGENT_DOCTOR_BIN value from the currently-installed plist.

    Returns None if the plist doesn't exist or the value isn't present.
    Used by migration paths to avoid overwriting a power-user's custom
    --agent-doctor-bin choice when rebuilding the helper.
    """

    plist = DEFAULT_PLIST_PATH
    if not plist.exists():
        return None
    try:
        with plist.open("rb") as fp:
            payload = plistlib.load(fp)
    except (OSError, plistlib.InvalidFileException):
        return None
    env = payload.get("EnvironmentVariables") or {}
    bin_path = env.get("AGENT_DOCTOR_BIN")
    return bin_path if isinstance(bin_path, str) else None


def status() -> dict[str, object]:
    """Report whether the plist, helper, and running agent are present.

    Defensive against missing ``launchctl`` (non-macOS, sandboxed env, broken
    PATH): if the subprocess raises :class:`FileNotFoundError` or other OS
    errors, treat the agent as "not running" rather than crashing the
    caller.
    """

    plist_exists = DEFAULT_PLIST_PATH.exists()
    helper_exists = DEFAULT_HELPER_PATH.exists()
    try:
        proc = _run_launchctl(
            ["launchctl", "print", f"{_domain_target()}/{LABEL}"]
        )
        running = proc.returncode == 0
    except (FileNotFoundError, OSError):
        running = False
    return {
        "plist": str(DEFAULT_PLIST_PATH),
        "plist_exists": plist_exists,
        "helper": str(DEFAULT_HELPER_PATH),
        "helper_exists": helper_exists,
        "running": running,
    }
