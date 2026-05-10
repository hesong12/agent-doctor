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

    # The Swift source must watch the sprite mtime inside the existing 15fps
    # timer and re-load NSImage when it changes.
    assert "attributesOfItem" in source
    assert ".modificationDate" in source
    # A cached last-mtime variable lives on the view so we don't reload every tick.
    assert "spriteMTime" in source or "lastSpriteMTime" in source
