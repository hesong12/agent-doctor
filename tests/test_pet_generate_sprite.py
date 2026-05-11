"""Tests for ``agent-doctor pet-generate-sprite``.

No live Gemini calls. We mock the SDK client so it returns canned PNG bytes,
then assert:

- The CLI shells out to the same ``transform_image`` + ``write_sprite_atomic``
  pair as ``pet-set-sprite``, with the sprite landing at the expected path.
- Missing key, SDK exception, decode failure, and write failure all exit
  non-zero — and never echo the key into stdout/stderr (redaction).
- The Gemini SDK call uses the Nano Banana 2 model id
  (``gemini-3-pro-image-preview``) confirmed via Context7 in the design phase.
"""

from __future__ import annotations

import io
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from agent_doctor import gemini_image, pet_display, settings as settings_mod  # noqa: E402

_FAKE_KEY = "FAKEKEY-NOT-A-REAL-GEMINI-CRED-NEVER-LEAKED-1234567890"


def _png_bytes(size: tuple[int, int] = (640, 640), color: tuple[int, int, int] = (200, 100, 50)) -> bytes:
    """Build a minimal PNG payload for the SDK mock to return."""

    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _white_bg_png_bytes(
    size: tuple[int, int] = (640, 640),
    bg: tuple[int, int, int] = (255, 255, 255),
    fg: tuple[int, int, int] = (220, 110, 40),
) -> bytes:
    """Synthesize a sticker-style PNG: solid background + colored disc.

    Mirrors what ``gemini-3-pro-image-preview`` actually returns for a
    "...sticker style, white background" prompt — and is the input shape
    the live smoke that blocked PR #19 acceptance #2 used to expose the
    floodfill sentinel bug.
    """

    from PIL import ImageDraw

    img = Image.new("RGB", size, bg)
    draw = ImageDraw.Draw(img)
    cx, cy = size[0] // 2, size[1] // 2
    radius = min(size) // 3
    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        fill=fg,
    )
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class _FakePart:
    def __init__(self, data: bytes) -> None:
        self.inline_data = type("InlineData", (), {"data": data, "mime_type": "image/png"})()
        self.text = None


class _FakeResponse:
    def __init__(self, image_bytes: bytes) -> None:
        self.parts = [_FakePart(image_bytes)]


class _FakeModels:
    def __init__(self, response: Any | Exception, recorder: dict[str, Any]) -> None:
        self._response = response
        self._recorder = recorder

    def generate_content(self, **kwargs: Any) -> Any:
        self._recorder["generate_content_kwargs"] = kwargs
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _FakeClient:
    def __init__(self, response: Any | Exception, recorder: dict[str, Any]) -> None:
        self._recorder = recorder
        self.models = _FakeModels(response, recorder)

    @classmethod
    def factory(cls, response: Any | Exception, recorder: dict[str, Any]) -> Any:
        def make(*, api_key: str | None = None, **_kwargs: Any) -> _FakeClient:
            recorder["api_key_seen"] = api_key
            return cls(response, recorder)

        return make


class _FakeImageConfig:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _FakeGenerateContentConfig:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _FakeTypes:
    GenerateContentConfig = _FakeGenerateContentConfig
    ImageConfig = _FakeImageConfig


class _FakeGenai:
    def __init__(self, response: Any | Exception, recorder: dict[str, Any]) -> None:
        self.Client = _FakeClient.factory(response, recorder)


def _patch_sdk(
    monkeypatch: pytest.MonkeyPatch,
    response: Any | Exception,
) -> dict[str, Any]:
    recorder: dict[str, Any] = {}
    fake_genai = _FakeGenai(response, recorder)

    def fake_load() -> tuple[Any, Any]:
        return fake_genai, _FakeTypes

    monkeypatch.setattr(gemini_image, "_load_genai", fake_load)
    return recorder


def _redirect_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "agent-doctor-home"
    monkeypatch.setattr(settings_mod, "_CONFIG_DIR", home)
    monkeypatch.setattr(settings_mod, "_CONFIG_FILE", home / "config.toml")
    return home


class _FakeKeyring:
    def __init__(self, initial: str | None = None) -> None:
        self.store: dict[tuple[str, str], str] = {}
        if initial is not None:
            self.store[(settings_mod._KEYRING_SERVICE, settings_mod._KEYRING_USERNAME)] = initial

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.store.pop((service, username), None)


def _install_fake_keyring(
    monkeypatch: pytest.MonkeyPatch, initial: str | None = None
) -> _FakeKeyring:
    fake = _FakeKeyring(initial)
    monkeypatch.setattr(settings_mod, "_try_import_keyring", lambda: fake)
    return fake


def test_pet_generate_sprite_happy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _redirect_settings(tmp_path, monkeypatch)
    _install_fake_keyring(monkeypatch, initial=_FAKE_KEY)

    out_path = tmp_path / "user-home" / ".agent-doctor" / "pet" / "sprite.png"
    monkeypatch.setattr(pet_display, "user_sprite_path", lambda: out_path)

    recorder = _patch_sdk(monkeypatch, _FakeResponse(_png_bytes()))

    # Spy on the existing pipeline functions so we KNOW they were called
    # rather than re-implemented.
    from agent_doctor import sprite_pipeline

    original_transform = sprite_pipeline.transform_image
    original_write = sprite_pipeline.write_sprite_atomic
    calls: dict[str, int] = {"transform": 0, "write": 0}

    def spy_transform(*args: Any, **kwargs: Any) -> Any:
        calls["transform"] += 1
        return original_transform(*args, **kwargs)

    def spy_write(*args: Any, **kwargs: Any) -> Any:
        calls["write"] += 1
        return original_write(*args, **kwargs)

    monkeypatch.setattr(sprite_pipeline, "transform_image", spy_transform)
    monkeypatch.setattr(sprite_pipeline, "write_sprite_atomic", spy_write)

    from agent_doctor import cli

    rc = cli.main(["pet-generate-sprite", "--prompt", "a cute orange tabby cat astronaut"])

    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert calls["transform"] == 1, "transform_image() must be reused, not re-implemented"
    assert calls["write"] == 1, "write_sprite_atomic() must be reused, not re-implemented"
    assert out_path.exists()
    with Image.open(out_path) as im:
        assert im.size == (sprite_pipeline.OUTPUT_SIZE, sprite_pipeline.OUTPUT_SIZE)
        assert im.mode == "RGBA"
        # Background removal applied: corner alpha should be 0.
        last = sprite_pipeline.OUTPUT_SIZE - 1
        for x, y in [(0, 0), (last, 0), (0, last), (last, last)]:
            _, _, _, alpha = im.load()[x, y]
            assert alpha == 0, f"corner ({x},{y}) should be transparent, got alpha={alpha}"
    # Key never echoed.
    assert _FAKE_KEY not in (captured.out + captured.err)
    # Recorded SDK call used the Nano Banana 2 model id and our key.
    assert recorder["api_key_seen"] == _FAKE_KEY
    assert recorder["generate_content_kwargs"]["model"] == gemini_image.NANO_BANANA_2_MODEL


def test_generate_pet_sprite_wraps_user_prompt_with_sticker_constraints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user's free-text prompt must be wrapped in the sticker preamble before
    it hits Gemini, otherwise the model returns a photoreal-scene image whose
    corners defeat the downstream floodfill (live regression: a forest-backed
    capuchin monkey came back fully opaque). Locks in the wrap so a future
    refactor cannot quietly drop it.
    """

    recorder = _patch_sdk(monkeypatch, _FakeResponse(_png_bytes()))

    user_subject = "a cheerful capuchin monkey holding a yellow flower"
    gemini_image.generate_pet_sprite_bytes(prompt=user_subject, api_key=_FAKE_KEY)

    sent = recorder["generate_content_kwargs"]["contents"]
    # The user's exact subject string survives verbatim — we wrap, not rewrite.
    assert user_subject in sent
    # And the load-bearing constraints from the preamble are present.
    assert gemini_image.STICKER_PROMPT_PREAMBLE.split("\n", 1)[0] in sent
    assert "pure-white background" in sent.lower() or "#ffffff" in sent.lower()
    assert "sticker" in sent.lower()
    # The wrapped prompt should be substantially longer than the raw subject.
    assert len(sent) > len(user_subject) + 200


def test_build_sticker_prompt_strips_and_appends() -> None:
    """Helper contract: user words appended verbatim after stripping whitespace,
    nothing else mutated.
    """

    wrapped = gemini_image.build_sticker_prompt("  a tiny rocket dog  \n")
    assert wrapped.startswith(gemini_image.STICKER_PROMPT_PREAMBLE)
    assert wrapped.endswith("a tiny rocket dog")
    assert "  a tiny rocket dog" not in wrapped  # leading whitespace was stripped


def test_pet_generate_sprite_explicit_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _redirect_settings(tmp_path, monkeypatch)
    _install_fake_keyring(monkeypatch, initial=_FAKE_KEY)
    _patch_sdk(monkeypatch, _FakeResponse(_png_bytes()))

    explicit = tmp_path / "explicit.png"

    from agent_doctor import cli

    rc = cli.main([
        "pet-generate-sprite",
        "--prompt",
        "a sleepy red panda",
        "--out",
        str(explicit),
    ])

    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert explicit.exists()
    assert _FAKE_KEY not in (captured.out + captured.err)


def test_pet_generate_sprite_no_bg_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _redirect_settings(tmp_path, monkeypatch)
    _install_fake_keyring(monkeypatch, initial=_FAKE_KEY)
    _patch_sdk(monkeypatch, _FakeResponse(_png_bytes()))

    out = tmp_path / "no-bg.png"

    from agent_doctor import cli

    rc = cli.main([
        "pet-generate-sprite",
        "--prompt",
        "a yellow rubber duck",
        "--out",
        str(out),
        "--no-bg-removal",
    ])

    captured = capsys.readouterr()
    assert rc == 0, captured.err
    with Image.open(out) as im:
        last = im.size[0] - 1
        # All four corners must stay opaque when background removal is off.
        for x, y in [(0, 0), (last, 0), (0, last), (last, last)]:
            _, _, _, alpha = im.load()[x, y]
            assert alpha == 255


def test_pet_generate_sprite_missing_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _redirect_settings(tmp_path, monkeypatch)
    _install_fake_keyring(monkeypatch, initial=None)

    from agent_doctor import cli

    rc = cli.main(["pet-generate-sprite", "--prompt", "anything"])

    captured = capsys.readouterr()
    assert rc != 0
    assert "settings set-gemini-key" in captured.err
    # Should never accidentally echo a key (we have none, but be defensive).
    assert _FAKE_KEY not in (captured.out + captured.err)


def test_pet_generate_sprite_sdk_raises_redacts_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _redirect_settings(tmp_path, monkeypatch)
    _install_fake_keyring(monkeypatch, initial=_FAKE_KEY)
    # SDK throws an exception that contains the key in its message — the
    # most dangerous case. The CLI must redact it before printing.
    _patch_sdk(monkeypatch, RuntimeError(f"upstream 401: bad key {_FAKE_KEY}"))

    from agent_doctor import cli

    rc = cli.main(["pet-generate-sprite", "--prompt", "doomed prompt"])

    captured = capsys.readouterr()
    assert rc != 0
    text = captured.out + captured.err
    assert _FAKE_KEY not in text, (
        f"API key MUST NOT leak into stderr — leaked text:\n{text}"
    )
    assert "REDACTED" in text or "redacted" in text or "***" in text or "Gemini" in text


def test_pet_generate_sprite_decode_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _redirect_settings(tmp_path, monkeypatch)
    _install_fake_keyring(monkeypatch, initial=_FAKE_KEY)
    _patch_sdk(monkeypatch, _FakeResponse(b"definitely not a real PNG"))

    from agent_doctor import cli

    rc = cli.main(["pet-generate-sprite", "--prompt", "broken response"])

    captured = capsys.readouterr()
    assert rc != 0
    text = (captured.out + captured.err).lower()
    assert "decode" in text or "image" in text
    assert _FAKE_KEY not in (captured.out + captured.err)
    assert "traceback" not in text


def test_pet_generate_sprite_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _redirect_settings(tmp_path, monkeypatch)
    _install_fake_keyring(monkeypatch, initial=_FAKE_KEY)
    _patch_sdk(monkeypatch, _FakeResponse(_png_bytes()))

    from agent_doctor import sprite_pipeline

    def fake_write(_image: Any, _dest: Any) -> Any:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(sprite_pipeline, "write_sprite_atomic", fake_write)

    out = tmp_path / "out.png"
    from agent_doctor import cli

    rc = cli.main([
        "pet-generate-sprite",
        "--prompt",
        "doomed write",
        "--out",
        str(out),
    ])

    captured = capsys.readouterr()
    assert rc != 0
    text = captured.err.lower()
    assert "write" in text
    assert "could not decode" not in text
    assert _FAKE_KEY not in (captured.out + captured.err)
    assert "traceback" not in text


def test_pet_generate_sprite_no_image_in_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _redirect_settings(tmp_path, monkeypatch)
    _install_fake_keyring(monkeypatch, initial=_FAKE_KEY)

    class _EmptyResponse:
        parts: list[Any] = []
        candidates: list[Any] = []

    _patch_sdk(monkeypatch, _EmptyResponse())

    from agent_doctor import cli

    rc = cli.main(["pet-generate-sprite", "--prompt", "weird empty response"])

    captured = capsys.readouterr()
    assert rc != 0
    text = captured.err.lower()
    assert "no image" in text or "prompt" in text or "gemini" in text
    assert _FAKE_KEY not in (captured.out + captured.err)


def test_pet_generate_sprite_white_background_e2e(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end regression for PR #19 acceptance #2.

    Mocks the Gemini SDK to return a sticker-shaped PNG (colored disc on
    pure-white background) — the deterministic shape that exposed the
    floodfill sentinel bug in live smoke. Asserts that:

    - `pet-generate-sprite` exits 0 and writes the sprite at `--out`
    - the four corners of the written 512×512 sprite have alpha=0
      (background removed)
    - the colored disc near the center is still opaque
    - the API key never leaks into stderr/stdout

    Without the sentinel fix in this commit, this test fails with
    ``corner alpha=255`` for every corner — exactly mirroring the live
    failure the reviewer reported.
    """

    _redirect_settings(tmp_path, monkeypatch)
    _install_fake_keyring(monkeypatch, initial=_FAKE_KEY)
    _patch_sdk(monkeypatch, _FakeResponse(_white_bg_png_bytes()))

    out = tmp_path / "white-bg-sprite.png"

    from agent_doctor import cli, sprite_pipeline

    rc = cli.main([
        "pet-generate-sprite",
        "--prompt",
        "a cute orange tabby cat astronaut, sticker style, white background",
        "--out",
        str(out),
    ])

    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert out.exists()
    assert _FAKE_KEY not in (captured.out + captured.err)

    with Image.open(out) as im:
        assert im.size == (sprite_pipeline.OUTPUT_SIZE, sprite_pipeline.OUTPUT_SIZE)
        assert im.mode == "RGBA"
        pixels = im.load()
        last = sprite_pipeline.OUTPUT_SIZE - 1
        for x, y in [(0, 0), (last, 0), (0, last), (last, last)]:
            _, _, _, alpha = pixels[x, y]
            assert alpha == 0, (
                f"PR #19 acceptance #2: white-background corner ({x},{y}) "
                f"must have alpha=0, got alpha={alpha}"
            )
        # The colored disc near the center stays opaque — bg removal didn't
        # nuke the sprite itself.
        cx = cy = sprite_pipeline.OUTPUT_SIZE // 2
        _, _, _, center_alpha = pixels[cx, cy]
        assert center_alpha == 255, (
            f"colored sprite at center should remain opaque, got alpha={center_alpha}"
        )


def test_pet_generate_sprite_near_white_background_e2e(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same as the white-bg e2e but for the (253,253,253) shape Gemini
    actually emits after PNG encoding. Was the original repro case in
    the live smoke that blocked PR #19."""

    _redirect_settings(tmp_path, monkeypatch)
    _install_fake_keyring(monkeypatch, initial=_FAKE_KEY)
    _patch_sdk(
        monkeypatch,
        _FakeResponse(_white_bg_png_bytes(bg=(253, 253, 253), fg=(60, 90, 200))),
    )

    out = tmp_path / "near-white-sprite.png"

    from agent_doctor import cli, sprite_pipeline

    rc = cli.main([
        "pet-generate-sprite",
        "--prompt",
        "a tiny blue robot, sticker style",
        "--out",
        str(out),
    ])

    assert rc == 0
    with Image.open(out) as im:
        pixels = im.load()
        last = sprite_pipeline.OUTPUT_SIZE - 1
        for x, y in [(0, 0), (last, 0), (0, last), (last, last)]:
            _, _, _, alpha = pixels[x, y]
            assert alpha == 0, (
                f"near-white (253,253,253) corner ({x},{y}) must have "
                f"alpha=0, got alpha={alpha}"
            )


def test_pet_generate_sprite_extracts_from_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Older SDK versions surface inline_data via candidates[].content.parts only."""

    _redirect_settings(tmp_path, monkeypatch)
    _install_fake_keyring(monkeypatch, initial=_FAKE_KEY)

    class _Candidate:
        def __init__(self, data: bytes) -> None:
            self.content = type(
                "C",
                (),
                {"parts": [_FakePart(data)]},
            )()

    class _CandidateResponse:
        parts: list[Any] = []

        def __init__(self, data: bytes) -> None:
            self.candidates = [_Candidate(data)]

    _patch_sdk(monkeypatch, _CandidateResponse(_png_bytes()))

    out = tmp_path / "out.png"
    from agent_doctor import cli

    rc = cli.main([
        "pet-generate-sprite",
        "--prompt",
        "fallback shape",
        "--out",
        str(out),
    ])

    assert rc == 0
    assert out.exists()


def test_appkit_menu_has_gemini_items() -> None:
    """The packaged Swift menu must include both new items alongside PR #18."""

    source = pet_display._appkit_source()

    # PR #18 items still present.
    assert "Change sprite..." in source
    assert "Reset to default" in source
    assert "Quit" in source

    # New Gemini items.
    assert "Generate sprite from prompt..." in source
    assert "Configure Gemini..." in source
    assert "@objc func generateSpriteFromPrompt" in source
    assert "@objc func configureGemini" in source

    # Generate path reuses the same hot-reload as PR #18 after shelling
    # `pet-generate-sprite --prompt ...`.
    assert "pet-generate-sprite" in source
    assert "reloadSpriteIfChanged" in source

    # Configure path must NOT pass the key in argv: it pipes via env var
    # and hands `--from-env` to the CLI.
    assert "settings" in source
    assert "set-gemini-key" in source
    assert "--from-env" in source
    assert "AGENT_DOCTOR_GEMINI_API_KEY" in source
    # Sanity: process.environment is set, not arguments.
    assert "process.environment = environment" in source

    # NSSecureTextField for the key entry, regular NSTextField for the prompt.
    assert "NSSecureTextField" in source
    assert "NSTextField" in source

    # Edit menu must be installed at app bootstrap so Cmd-V works inside the
    # NSAlert text fields under the .accessory activation policy.
    assert "NSText.paste(_:)" in source
    assert "NSText.cut(_:)" in source
    assert "NSText.copy(_:)" in source
    assert "NSText.selectAll(_:)" in source
    assert "app.mainMenu = mainMenu" in source

    # Generation activity HUD — spinning indicator while the Gemini
    # subprocess runs, started on the main thread before dispatch and
    # always stopped before the success/error branch on the way back.
    assert "NSProgressIndicator" in source
    assert "startGenerationIndicator()" in source
    assert "stopGenerationIndicator()" in source
    assert ".isIndeterminate = true" in source
