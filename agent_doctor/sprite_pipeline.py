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
# Two complementary sentinels so we can identify floodfilled pixels by
# "took on BOTH values across two passes" rather than "value-equals-A". A
# pixel that originally already equaled either sentinel can still be
# unambiguously flagged as filled, because no untouched pixel can equal
# both sentinels (they differ in every channel).
_FLOODFILL_SENTINEL = (1, 2, 3)
_FLOODFILL_SENTINEL_ALT = (254, 253, 252)
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


def _exact_match_mask(image: "PILImage", color: tuple[int, int, int]) -> "PILImage":
    """Return an L-mode mask: 255 where ``image[p] == color`` exactly, else 0.

    Vectorized: builds a same-size solid color image, takes the per-channel
    ``ImageChops.difference``, collapses to a single per-pixel "max channel
    diff" via ``ImageChops.lighter``, then maps zero-diff pixels to 255.
    """

    Image, _ImageDraw, _ImageFilter = _load_pillow()
    from PIL import ImageChops

    sentinel_image = Image.new(image.mode, image.size, color)
    diff = ImageChops.difference(image, sentinel_image)
    channels = diff.split()
    max_diff = channels[0]
    for ch in channels[1:]:
        max_diff = ImageChops.lighter(max_diff, ch)
    return max_diff.point(lambda p: 255 if p == 0 else 0)


def _floodfill_alpha(rgba: "PILImage") -> "PILImage":
    """Replace cream/uniform corners with transparency.

    Mirrors the manual swap pipeline: floodfill from each corner with a
    sentinel color, derive a binary alpha from "did floodfill reach this
    pixel?", then soften with a small Gaussian blur to avoid hard fringes.

    Identifying the filled region needs care. Two prior approaches were both
    wrong on different edge cases:

    1. ``pixel == sentinel`` punches out any legitimate detail whose color
       happens to quantize to the sentinel triple after JPEG + resize, even
       when floodfill never reached it.
    2. ``pixel changed vs. original`` (via ``ImageChops.difference``) misses
       the symmetric case: pixels that floodfill *did* reach but that already
       equaled the sentinel — those pixels stay value-equal across the
       operation and look "untouched", so the corner-connected background
       leaves opaque artifacts when it happens to contain sentinel-colored
       pixels.

    We sidestep both by floodfilling **twice** with two complementary
    sentinels (``A = (1,2,3)`` and ``B = (254,253,252)``, no overlap on any
    channel) and intersecting the per-pass exact-match masks. A pixel is
    flagged "filled" iff ``target_a[p] == A AND target_b[p] == B``. Since
    a single original pixel cannot simultaneously equal both sentinels, the
    only way both equalities hold is that floodfill painted the pixel in
    both passes — the desired ground truth. Untouched pixels never satisfy
    both equalities even when they happen to equal one sentinel.
    """

    _Image, ImageDraw, ImageFilter = _load_pillow()
    from PIL import ImageChops  # PIL is already importable per _load_pillow()

    rgb = rgba.convert("RGB")
    width, height = rgb.size

    target_a = rgb.copy()
    target_b = rgb.copy()
    seeds = [(0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)]
    for seed in seeds:
        ImageDraw.floodfill(target_a, seed, _FLOODFILL_SENTINEL, thresh=_FLOODFILL_THRESH)
        ImageDraw.floodfill(target_b, seed, _FLOODFILL_SENTINEL_ALT, thresh=_FLOODFILL_THRESH)

    match_a = _exact_match_mask(target_a, _FLOODFILL_SENTINEL)
    match_b = _exact_match_mask(target_b, _FLOODFILL_SENTINEL_ALT)
    # Per-pixel min: 255 only where both matches hold (= floodfilled).
    filled = ImageChops.darker(match_a, match_b)

    # alpha: 0 where filled (transparent), 255 where untouched (opaque).
    alpha = ImageChops.invert(filled)
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


def write_sprite_atomic(image: "PILImage", destination: Path) -> Path:
    """Atomically write a transformed PIL image to ``destination``.

    Uses ``NamedTemporaryFile`` in the destination's parent directory plus
    ``Path.replace`` so a concurrent reader of the destination always sees
    either the previous PNG or the new one — never a partial.

    Errors raised here come from the **write** side of the pipeline (mkdir,
    PNG encode, atomic rename) — destination permissions, ENOSPC, read-only
    target dir, and so on — not from decoding the source. CLI callers
    should handle this exception path separately so they can report the
    correct root cause.
    """

    dest = Path(destination).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)

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


def apply_sprite(
    source: Path,
    destination: Path,
    *,
    remove_background: bool = True,
) -> Path:
    """Run the pipeline and atomically write the result to ``destination``.

    Returns the destination path on success. Convenience wrapper for
    programmatic callers; the CLI splits decode and write into separate
    error-handling phases via :func:`transform_image` and
    :func:`write_sprite_atomic` directly.
    """

    image = transform_image(source, remove_background=remove_background)
    return write_sprite_atomic(image, destination)
