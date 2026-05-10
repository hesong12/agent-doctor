"""Custom pet sprite image pipeline.

Takes any Pillow-readable image and produces a 512x512 RGBA PNG suitable for
the Agent Doctor desktop pet. The transform is:

1. Center-square crop.
2. LANCZOS resize to ``OUTPUT_SIZE``.
3. (Optional) Floodfill-based background removal seeded from the four corners
   with ``thresh=28``, then a soft GaussianBlur(0.6) on the alpha to soften
   fringes. Skipped when ``remove_background=False``.
4. Atomic write: temp file in the destination directory, then ``Path.replace``.

Pillow is an optional extra (``pip install agent-doctor[sprite]``). It is
imported lazily inside :func:`_load_pillow`; importing this module does not
require Pillow until you actually run the transform.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - type-checking only
    from PIL.Image import Image as PILImage

OUTPUT_SIZE = 512
_FLOODFILL_THRESH = 28
_FLOODFILL_SENTINEL = (1, 2, 3)
_ALPHA_BLUR_RADIUS = 0.6


class PillowMissingError(RuntimeError):
    """Raised when Pillow is not installed but the pipeline needs it."""


def _load_pillow() -> Any:
    try:
        from PIL import Image, ImageDraw, ImageFilter
    except ImportError as exc:
        raise PillowMissingError(
            "Pillow is required for the custom-sprite pipeline. "
            "Install with `pip install agent-doctor[sprite]`."
        ) from exc
    return Image, ImageDraw, ImageFilter


def _center_square_crop(img: "PILImage") -> "PILImage":
    width, height = img.size
    if width == height:
        return img
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    return img.crop((left, top, left + side, top + side))


def _floodfill_alpha(rgba: "PILImage") -> "PILImage":
    """Replace cream/uniform corners with transparency.

    Mirrors the manual swap pipeline: floodfill an RGB copy from each corner
    with a sentinel color, derive a binary alpha from "is sentinel?", then
    soften with a small Gaussian blur to avoid hard fringes.

    The alpha mask is built with vectorized Pillow ops (``Image.split``,
    ``Image.point``, ``ImageChops.lighter``) instead of a Python pixel loop:
    each channel becomes a 0/255 mask of "this channel matches the sentinel
    byte", and ``lighter`` (per-pixel max) gives 0 only when **all three**
    channel masks are 0 — i.e. the pixel equals the exact sentinel triple.
    """

    _Image, ImageDraw, ImageFilter = _load_pillow()
    from PIL import ImageChops  # PIL is already importable per _load_pillow()

    rgb = rgba.convert("RGB")
    width, height = rgb.size

    target = rgb.copy()
    for seed in [(0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)]:
        ImageDraw.floodfill(target, seed, _FLOODFILL_SENTINEL, thresh=_FLOODFILL_THRESH)

    r, g, b = target.split()
    sentinel_r, sentinel_g, sentinel_b = _FLOODFILL_SENTINEL
    rmask = r.point(lambda p, s=sentinel_r: 0 if p == s else 255)
    gmask = g.point(lambda p, s=sentinel_g: 0 if p == s else 255)
    bmask = b.point(lambda p, s=sentinel_b: 0 if p == s else 255)
    alpha = ImageChops.lighter(ImageChops.lighter(rmask, gmask), bmask)

    alpha = alpha.filter(ImageFilter.GaussianBlur(radius=_ALPHA_BLUR_RADIUS))

    out = rgba.copy()
    out.putalpha(alpha)
    return out


def transform_image(source: Path, *, remove_background: bool = True) -> "PILImage":
    """Run the full sprite transform and return the resulting RGBA image.

    The image is not written to disk; use :func:`apply_sprite` for that.
    """

    Image, _ImageDraw, _ImageFilter = _load_pillow()
    src_path = Path(source).expanduser()
    if not src_path.exists():
        raise FileNotFoundError(f"Source image not found: {src_path}")

    with Image.open(src_path) as opened:
        opened.load()
        cropped = _center_square_crop(opened)
        resized = cropped.resize((OUTPUT_SIZE, OUTPUT_SIZE), Image.LANCZOS)

    rgba = resized.convert("RGBA")
    if remove_background:
        return _floodfill_alpha(rgba)
    return rgba


def apply_sprite(
    source: Path,
    destination: Path,
    *,
    remove_background: bool = True,
) -> Path:
    """Run the pipeline and atomically write the result to ``destination``.

    Returns the destination path on success.
    """

    dest = Path(destination).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)

    image = transform_image(source, remove_background=remove_background)

    tmp = NamedTemporaryFile(
        prefix=".sprite-",
        suffix=".png.tmp",
        dir=str(dest.parent),
        delete=False,
    )
    tmp_path = Path(tmp.name)
    tmp.close()
    try:
        image.save(tmp_path, format="PNG")
        tmp_path.replace(dest)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    return dest
