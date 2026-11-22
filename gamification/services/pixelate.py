"""Quiz images (spec section 5.14): a photo, sharp or pixelated, as JPEG bytes.

Timed quiz kinds reveal the photo in steps: level 0 is the coarsest pixelation and
`SHARP` is the clean photo. The image is made on the server, so the sharp photo never
reaches the browser before its time.
"""

from io import BytesIO

from PIL import Image

from scanner.services.images import photo_path, thumb_path

MAX_SIDE = 800
# Blocks across the longer side for each level; the level after the last one is sharp.
LEVEL_BLOCKS = (6, 12, 20, 32, 64)
SHARP = len(LEVEL_BLOCKS)


def source_path(photo):
    """The 1600 px thumbnail, or the photo itself if there is no thumbnail; None if
    neither file exists."""
    for path in (thumb_path(photo.file_name, 1600), photo_path(photo.file_name)):
        if path.exists():
            return path
    return None


def pixelate(image, blocks):
    """`image` reduced to `blocks` blocks across its longer side, back at its size."""
    width, height = image.size
    scale = blocks / max(width, height)
    small = image.resize(
        (max(1, round(width * scale)), max(1, round(height * scale))), Image.Resampling.BOX
    )
    return small.resize((width, height), Image.Resampling.NEAREST)


def render(photo, level=SHARP):
    """JPEG bytes of `photo` at `level` (0 … SHARP), at most MAX_SIDE px; None if the
    photo file is missing."""
    path = source_path(photo)
    if path is None:
        return None
    with Image.open(path) as source:
        image = source.convert("RGB")
    image.thumbnail((MAX_SIDE, MAX_SIDE))
    if level < SHARP:
        image = pixelate(image, LEVEL_BLOCKS[max(0, level)])
    buffer = BytesIO()
    image.save(buffer, "JPEG", quality=85)
    return buffer.getvalue()
