"""Gemini image-generation client for the desktop pet pipeline.

Thin wrapper around the official ``google-genai`` Python SDK. The model used
is ``gemini-3-pro-image-preview`` (the late-2025 "Nano Banana 2" / Nano
Banana Pro family). The verified call shape comes from
https://github.com/googleapis/python-genai (Context7 ``/googleapis/python-genai``,
keyed query "image generation gemini-3-pro-image-preview" / "client api_key").

The SDK is optional — install with ``pip install agent-doctor[gemini]`` — so
this module imports it lazily inside :func:`generate_pet_sprite_bytes`.

Error handling rule: the caller-supplied ``api_key`` MUST NOT leak into
exception messages we raise from here. We redact it via
:func:`agent_doctor.settings.redact_secret` before re-raising.
"""

from __future__ import annotations

from typing import Any

from .settings import SettingsError, redact_secret

# Verified via context7 /googleapis/python-genai (late 2025): the "Nano Banana
# Pro / Nano Banana 2" image-gen model id. Keep this literal in one place so a
# future SDK refresh has a single point of edit.
NANO_BANANA_2_MODEL = "gemini-3-pro-image-preview"

# Wrapping a naive user prompt with these constraints turns "a cute orange tabby
# cat astronaut" into something the downstream corner-floodfill bg removal can
# actually cut out. Without it, Gemini happily renders the subject in a
# photorealistic forest / studio / scene, the corners end up full of detail,
# and _floodfill_alpha leaves the entire image opaque (live regression seen in
# review). The constraints are stacked from most-load-bearing (single subject,
# flat white background) downward so a model that follows only the leading
# clauses still produces a cuttable sprite.
STICKER_PROMPT_PREAMBLE = (
    "Render the requested subject as a single centered character sticker on a "
    "pure-white background.\n"
    "\n"
    "Hard constraints (must follow exactly):\n"
    "- One subject only, centered, fully visible, no cropping at the edges.\n"
    "- Plain solid pure-white background (#FFFFFF). No scenery, no environment, "
    "no props, no foliage, no rim lighting, no shadows on the background.\n"
    "- Flat illustration / sticker style with clean outlines and simple shading. "
    "No photographic detail, no painterly textures, no depth-of-field blur.\n"
    "- Square 1:1 framing with a small even margin between the subject and the "
    "image edge.\n"
    "- No text, no watermarks, no signatures, no captions.\n"
    "\n"
    "Subject: "
)


def build_sticker_prompt(user_prompt: str) -> str:
    """Return ``user_prompt`` prefixed with the sticker-style constraints.

    Exported so callers and tests can inspect the exact text sent to Gemini.
    The user's words are appended verbatim (after stripping surrounding
    whitespace); the preamble is responsible for the style discipline.
    """

    return STICKER_PROMPT_PREAMBLE + user_prompt.strip()


class GeminiSdkMissingError(RuntimeError):
    """Raised when ``google-genai`` is not installed but the pipeline needs it."""


class GeminiImageError(RuntimeError):
    """Raised when the Gemini image-gen call fails or returns no image part.

    The API key is always scrubbed from the message before this is raised.
    """


def _load_genai() -> Any:
    try:
        from google import genai  # type: ignore[import-not-found]
        from google.genai import types  # type: ignore[import-not-found]
    except ImportError as exc:
        raise GeminiSdkMissingError(
            "google-genai is required for pet-generate-sprite. "
            "Install with `pip install agent-doctor[gemini]`."
        ) from exc
    return genai, types


def _extract_first_image_bytes(response: Any) -> bytes:
    """Pull the first ``inline_data`` image payload out of a generate_content response.

    Walks the documented shape (``response.parts[*].inline_data.data``) and
    falls back to ``response.candidates[*].content.parts[*].inline_data.data``
    for older SDK versions that didn't surface the convenience ``parts``
    accessor.
    """

    direct_parts = getattr(response, "parts", None)
    if direct_parts:
        for part in direct_parts:
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None) if inline is not None else None
            if data:
                return bytes(data)

    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) if content is not None else None
        if not parts:
            continue
        for part in parts:
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None) if inline is not None else None
            if data:
                return bytes(data)

    raise GeminiImageError(
        "Gemini returned no image bytes for this prompt. "
        "Try a more concrete subject or simpler description."
    )


def generate_pet_sprite_bytes(
    *,
    prompt: str,
    api_key: str,
    model: str = NANO_BANANA_2_MODEL,
) -> bytes:
    """Return raw image bytes (PNG/JPEG) for ``prompt`` from Gemini Nano Banana 2.

    Parameters
    ----------
    prompt:
        Text description of the desktop pet to generate.
    api_key:
        The Gemini API key. Loaded by the CLI from the settings module — never
        accepted as a positional CLI argument.
    model:
        Image-gen model id. Defaults to the late-2025 Nano Banana Pro family.
    """

    if not prompt or not prompt.strip():
        raise GeminiImageError("Prompt is empty.")
    if not api_key:
        raise GeminiImageError("Gemini API key is missing.")

    genai, types = _load_genai()

    try:
        client = genai.Client(api_key=api_key)
    except Exception as exc:
        raise GeminiImageError(
            f"Could not initialise Gemini client: {redact_secret(str(exc), api_key)}"
        ) from None

    # Square aspect ratio aligns with the downstream center-square crop in
    # sprite_pipeline.transform_image — we save the model from generating
    # extra letterboxed pixels we'd immediately crop off.
    try:
        config = types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            image_config=types.ImageConfig(aspect_ratio="1:1"),
        )
    except Exception:
        # Some older SDK revs lack response_modalities on this surface.
        # Falling back to default config still returns IMAGE parts for
        # image-gen models.
        config = None

    # Wrap the user prompt with sticker-style constraints so the model produces
    # a clean cut-out-able subject on a flat white background instead of a
    # photorealistic scene whose corners defeat the downstream floodfill.
    wrapped_prompt = build_sticker_prompt(prompt)

    try:
        kwargs: dict[str, Any] = {"model": model, "contents": wrapped_prompt}
        if config is not None:
            kwargs["config"] = config
        response = client.models.generate_content(**kwargs)
    except Exception as exc:
        raise GeminiImageError(
            f"Gemini image-gen call failed: {redact_secret(str(exc), api_key)}"
        ) from None

    try:
        return _extract_first_image_bytes(response)
    except GeminiImageError:
        raise
    except Exception as exc:
        raise GeminiImageError(
            f"Could not parse Gemini response: {redact_secret(str(exc), api_key)}"
        ) from None


__all__ = [
    "GeminiImageError",
    "GeminiSdkMissingError",
    "NANO_BANANA_2_MODEL",
    "STICKER_PROMPT_PREAMBLE",
    "SettingsError",
    "build_sticker_prompt",
    "generate_pet_sprite_bytes",
]
