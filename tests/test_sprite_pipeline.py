"""Tests for the custom pet sprite pipeline.

Covers the in-memory transform (center-square crop -> 512x512 RGBA -> corner
floodfill alpha) plus atomic write semantics, ``pet_asset_path()`` preference,
and the CLI's behaviour when Pillow is missing.
"""

from __future__ import annotations

import builtins
import importlib
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageDraw  # noqa: E402

from agent_doctor import pet_display, sprite_pipeline  # noqa: E402

SIZE = sprite_pipeline.OUTPUT_SIZE


def _cream_cat_image(width: int = 800, height: int = 1100) -> Image.Image:
    """Build a synthetic JPEG-like source image: cream paper + brown blob."""

    img = Image.new("RGB", (width, height), (250, 244, 230))
    draw = ImageDraw.Draw(img)
    cx, cy = width // 2, height // 2
    radius = min(width, height) // 3
    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        fill=(120, 80, 50),
    )
    return img


def test_transform_returns_512_rgba(tmp_path: Path) -> None:
    src = tmp_path / "src.jpg"
    _cream_cat_image().save(src, "JPEG", quality=92)

    out = sprite_pipeline.transform_image(src)

    assert out.mode == "RGBA"
    assert out.size == (SIZE, SIZE)


def test_transform_floodfills_corners_to_transparent(tmp_path: Path) -> None:
    src = tmp_path / "src.jpg"
    _cream_cat_image().save(src, "JPEG", quality=92)

    out = sprite_pipeline.transform_image(src)
    pixels = out.load()

    last = SIZE - 1
    for x, y in [(0, 0), (last, 0), (0, last), (last, last)]:
        _, _, _, alpha = pixels[x, y]
        assert alpha == 0, f"corner ({x},{y}) should be transparent, got alpha={alpha}"


def test_transform_handles_sentinel_colored_corner_background(tmp_path: Path) -> None:
    """Regression: the corner-connected background may itself already equal
    the floodfill sentinel triple ``(1, 2, 3)``. A diff-based mask alone
    misses those pixels because floodfill leaves them value-unchanged. The
    two-pass approach (intersect exact-match masks for two complementary
    sentinels) correctly flags them as filled, so the entire background
    becomes transparent.
    """

    # Whole image is exactly the primary sentinel color. Floodfill from any
    # corner walks the entire image (every pixel is within thresh of every
    # other), so 100% of pixels should end up transparent.
    img = Image.new("RGB", (64, 64), sprite_pipeline._FLOODFILL_SENTINEL)
    src = tmp_path / "all-sentinel.png"
    img.save(src, "PNG")

    out = sprite_pipeline.transform_image(src)
    pixels = out.load()

    last = SIZE - 1
    for x, y in [(0, 0), (last, 0), (0, last), (last, last), (SIZE // 2, SIZE // 2)]:
        _, _, _, alpha = pixels[x, y]
        assert alpha == 0, (
            f"pixel ({x},{y}) should be transparent for an all-sentinel "
            f"background, got alpha={alpha}"
        )


def test_transform_does_not_punch_out_unfilled_sentinel_colored_pixels(
    tmp_path: Path,
) -> None:
    """Regression: a pixel with RGB == (1,2,3) that floodfill never reached
    must stay opaque. The mask is keyed on "did floodfill change this pixel?",
    not on "is this pixel exactly equal to the sentinel triple?", so legit
    dark details that happen to land on the sentinel after JPEG/resize don't
    get punched out.
    """

    # Build a 64×64 image with a cream background that floodfill from a
    # corner WILL reach, plus a single dark "detail" pixel at (32, 32) that
    # is exactly the sentinel triple (1, 2, 3). The detail pixel is far
    # enough from the cream background (color distance ≫ thresh=28) that
    # floodfill will not flood through it.
    width, height = 64, 64
    img = Image.new("RGB", (width, height), (250, 244, 230))
    img.putpixel((width // 2, height // 2), (1, 2, 3))

    src = tmp_path / "sentinel-detail.png"
    img.save(src, "PNG")

    out = sprite_pipeline.transform_image(src)
    pixels = out.load()

    # Corners should be transparent (cream background was filled).
    last = SIZE - 1
    for x, y in [(0, 0), (last, 0), (0, last), (last, last)]:
        _, _, _, alpha = pixels[x, y]
        assert alpha == 0, f"corner ({x},{y}) should be transparent, got alpha={alpha}"

    # The detail pixel area, scaled up to 512x512, must remain opaque even
    # though the original RGB matches the floodfill sentinel exactly.
    cx = SIZE // 2
    cy = SIZE // 2
    _, _, _, alpha = pixels[cx, cy]
    assert alpha == 255, (
        f"sentinel-colored detail at center ({cx},{cy}) was incorrectly "
        f"punched out: alpha={alpha}"
    )


def test_transform_no_bg_removal_keeps_corners_opaque(tmp_path: Path) -> None:
    src = tmp_path / "src.jpg"
    _cream_cat_image().save(src, "JPEG", quality=92)

    out = sprite_pipeline.transform_image(src, remove_background=False)
    pixels = out.load()

    last = SIZE - 1
    for x, y in [(0, 0), (last, 0), (0, last), (last, last)]:
        _, _, _, alpha = pixels[x, y]
        assert alpha == 255, f"corner ({x},{y}) should stay opaque, got alpha={alpha}"


def test_apply_writes_512_rgba_png(tmp_path: Path) -> None:
    src = tmp_path / "src.jpg"
    _cream_cat_image().save(src, "JPEG", quality=92)
    dest = tmp_path / "sprite.png"

    written = sprite_pipeline.apply_sprite(src, dest)

    assert written == dest
    assert dest.exists()
    with Image.open(dest) as im:
        assert im.format == "PNG"
        assert im.mode == "RGBA"
        assert im.size == (SIZE, SIZE)


def test_apply_creates_parent_directories(tmp_path: Path) -> None:
    src = tmp_path / "src.jpg"
    _cream_cat_image().save(src, "JPEG", quality=92)
    dest = tmp_path / "nested" / "dirs" / "sprite.png"

    sprite_pipeline.apply_sprite(src, dest)

    assert dest.exists()


def test_apply_atomic_write_no_partial_reads(tmp_path: Path) -> None:
    """Concurrent reads while we replace the file must always see a complete PNG."""

    src = tmp_path / "src.jpg"
    _cream_cat_image().save(src, "JPEG", quality=92)
    dest = tmp_path / "sprite.png"
    # Seed an initial valid file so a concurrent reader has something to open.
    sprite_pipeline.apply_sprite(src, dest)

    stop = threading.Event()
    errors: list[Exception] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                with Image.open(dest) as im:
                    im.load()
            except Exception as exc:  # pragma: no cover - failure is the assertion
                errors.append(exc)
                return

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        for _ in range(8):
            sprite_pipeline.apply_sprite(src, dest)
            time.sleep(0.01)
    finally:
        stop.set()
        thread.join(timeout=2)

    assert not errors, f"reader saw partial/corrupt files: {errors!r}"


def test_apply_atomic_uses_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pipeline must call ``Path.replace`` (atomic on POSIX) for the swap."""

    src = tmp_path / "src.jpg"
    _cream_cat_image().save(src, "JPEG", quality=92)
    dest = tmp_path / "sprite.png"

    calls: list[Path] = []
    real_replace = Path.replace

    def spy(self: Path, target: str | os.PathLike[str]) -> Path:
        calls.append(Path(target))
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", spy)

    sprite_pipeline.apply_sprite(src, dest)

    assert any(Path(c) == dest for c in calls), f"expected Path.replace -> {dest}, got {calls!r}"


def test_pet_asset_path_prefers_user_sprite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    user_dir = tmp_path / "agent-doctor-home" / "pet"
    user_dir.mkdir(parents=True)
    sprite = user_dir / "sprite.png"
    Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0)).save(sprite, "PNG")

    monkeypatch.setattr(pet_display, "user_sprite_path", lambda: sprite)

    resolved = pet_display.pet_asset_path()

    assert resolved == sprite


def test_pet_asset_path_falls_back_to_packaged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    missing = tmp_path / "missing-sprite.png"
    monkeypatch.setattr(pet_display, "user_sprite_path", lambda: missing)

    resolved = pet_display.pet_asset_path()

    assert resolved is not None
    assert resolved.name == "doctor_pet.png"
    assert resolved.exists()


def test_user_sprite_path_default_location() -> None:
    path = pet_display.user_sprite_path()

    assert path == Path("~/.agent-doctor/pet/sprite.png").expanduser()


def test_cli_pet_set_sprite_writes_to_user_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "src.jpg"
    _cream_cat_image().save(src, "JPEG", quality=92)
    out_path = tmp_path / "user-home" / ".agent-doctor" / "pet" / "sprite.png"

    monkeypatch.setattr(pet_display, "user_sprite_path", lambda: out_path)

    from agent_doctor import cli

    rc = cli.main(["pet-set-sprite", str(src)])

    assert rc == 0
    assert out_path.exists()
    with Image.open(out_path) as im:
        assert im.size == (SIZE, SIZE)
        assert im.mode == "RGBA"


def test_cli_pet_set_sprite_explicit_out(tmp_path: Path) -> None:
    src = tmp_path / "src.jpg"
    _cream_cat_image().save(src, "JPEG", quality=92)
    out = tmp_path / "explicit.png"

    from agent_doctor import cli

    rc = cli.main(["pet-set-sprite", str(src), "--out", str(out)])

    assert rc == 0
    assert out.exists()


def test_cli_pet_set_sprite_missing_input(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from agent_doctor import cli

    rc = cli.main(["pet-set-sprite", str(tmp_path / "does-not-exist.jpg")])

    captured = capsys.readouterr()
    assert rc != 0
    assert "not found" in (captured.err + captured.out).lower()


def test_cli_pet_set_sprite_directory_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A directory as the source must produce a clean message + nonzero exit,
    not a Python traceback (which would surface as an ugly Tk messagebox
    when invoked from the right-click flow)."""

    from agent_doctor import cli

    rc = cli.main(["pet-set-sprite", str(tmp_path)])

    captured = capsys.readouterr()
    text = (captured.err + captured.out).lower()
    assert rc != 0
    assert "directory" in text
    assert "traceback" not in text


def test_cli_pet_set_sprite_write_error_is_reported_as_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression: an OSError from the WRITE phase (e.g. ENOSPC, read-only
    destination dir) must be reported as a write failure, not a decode
    failure. Pre-fix, the broad ``except OSError`` around ``apply_sprite``
    swallowed both phases and reported every error as ``could not decode
    image '<source>'``, which sent the user to the wrong root cause.
    """

    src = tmp_path / "src.jpg"
    _cream_cat_image().save(src, "JPEG", quality=92)
    out_path = tmp_path / "dest" / "sprite.png"

    from agent_doctor import sprite_pipeline

    def fake_write(_image: object, _destination: object) -> Path:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(sprite_pipeline, "write_sprite_atomic", fake_write)

    from agent_doctor import cli

    rc = cli.main(["pet-set-sprite", str(src), "--out", str(out_path)])

    captured = capsys.readouterr()
    text = (captured.err + captured.out).lower()

    assert rc != 0
    assert "traceback" not in text
    # Must clearly point at the destination/write side, not the source decode.
    assert "could not decode" not in text
    assert "write" in text
    assert str(out_path).lower() in text


def test_cli_pet_set_sprite_corrupt_image(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Non-image files (e.g., a .png that is not actually a PNG) should
    surface PIL.UnidentifiedImageError as a single clean error line, not
    a stack trace."""

    src = tmp_path / "not-really.png"
    src.write_bytes(b"definitely not an image, just some garbage bytes")
    out = tmp_path / "out.png"

    from agent_doctor import cli

    rc = cli.main(["pet-set-sprite", str(src), "--out", str(out)])

    captured = capsys.readouterr()
    text = (captured.err + captured.out).lower()
    assert rc != 0
    assert "traceback" not in text
    # Some indication that decoding failed (don't pin the exact PIL phrasing).
    assert "decode" in text or "image" in text


def test_cli_pet_set_sprite_clean_error_when_pillow_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    src = tmp_path / "src.jpg"
    _cream_cat_image().save(src, "JPEG", quality=92)

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object):  # type: ignore[override]
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("No module named 'PIL'")
        return real_import(name, *args, **kwargs)

    # Drop any cached PIL modules so the next import goes through fake_import.
    for mod in list(sys.modules):
        if mod == "PIL" or mod.startswith("PIL."):
            sys.modules.pop(mod, None)
    # And drop the cached sprite_pipeline so its lazy import re-runs.
    sys.modules.pop("agent_doctor.sprite_pipeline", None)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    from agent_doctor import cli

    rc = cli.main(["pet-set-sprite", str(src), "--out", str(tmp_path / "sprite.png")])

    captured = capsys.readouterr()
    text = (captured.err + captured.out).lower()
    assert rc != 0
    assert "pillow" in text or "agent-doctor[sprite]" in text

    # Restore real PIL for downstream tests in this process.
    monkeypatch.setattr(builtins, "__import__", real_import)
    sys.modules.pop("agent_doctor.sprite_pipeline", None)
    importlib.import_module("agent_doctor.sprite_pipeline")


def test_appkit_display_source_hot_reloads_sprite() -> None:
    source = pet_display._appkit_source()

    # The Swift source must watch the sprite mtime and re-load NSImage when
    # it changes. The check must be throttled to the status-poll cadence
    # (called from inside the poll-interval branch) rather than every tick.
    assert "attributesOfItem" in source
    assert ".modificationDate" in source
    assert "spriteMTime" in source or "lastSpriteMTime" in source

    # The hot-reload loop must re-resolve "user override exists?" each tick
    # via currentSpritePath() so a sprite installed AFTER launch is picked up
    # without a restart, even when the window started on the packaged default
    # (and so the pet reverts to packaged when the user sprite is deleted).
    assert "userSpritePath" in source
    assert "packagedSpritePath" in source
    assert "func currentSpritePath()" in source
    assert "FileManager.default.fileExists(atPath: userSpritePath)" in source

    # The initial paint must also go through the resolver, not the
    # launch-time assetPath, so a window launched on the packaged sprite
    # picks up an existing user override on its very first frame.
    assert "view.reloadSpriteIfChanged()" in source


def test_display_pet_appkit_passes_both_sprite_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """The AppKit launcher must pass both the user-override and packaged paths."""

    captured: dict[str, list[str]] = {}

    class _FakeCompleted:
        returncode = 0

    def fake_run(command: list[str], check: bool = False) -> _FakeCompleted:  # type: ignore[no-untyped-def]
        captured["command"] = list(command)
        return _FakeCompleted()

    monkeypatch.setattr(pet_display.subprocess, "run", fake_run)

    pet_display._display_pet_appkit(
        Path("/tmp/agent-doctor-fake-status.json"),
        poll_seconds=1.0,
        topmost=True,
        asset_path=None,
    )

    command = captured["command"]
    user_sprite = str(pet_display.user_sprite_path())
    packaged = pet_display.packaged_sprite_path()

    assert user_sprite in command, command
    if packaged is not None:
        assert str(packaged) in command, command
