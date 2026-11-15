"""Photo files and thumbnails on disk."""

from pathlib import Path

import cv2
from django.conf import settings
from PIL import Image

JPEG_QUALITY = 95


def photo_path(file_name):
    return Path(settings.PHOTOS_DIR) / file_name


def thumb_name(file_name, size):
    return f"{Path(file_name).stem}_{size}.jpg"


def thumb_path(file_name, size):
    return Path(settings.THUMBS_DIR) / thumb_name(file_name, size)


def save_photo(image, file_name):
    """Write a BGR image as JPEG (quality 95) atomically and return its path."""
    path = photo_path(file_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        raise OSError(f"Could not encode {file_name}")
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(buffer.tobytes())
    tmp.replace(path)
    return path


def make_thumbnails(file_name, sizes=None):
    """Create width-bounded thumbnails (400 and 1600 px by default) for srcset."""
    sizes = sizes or settings.THUMBNAIL_SIZES
    source = photo_path(file_name)
    Path(settings.THUMBS_DIR).mkdir(parents=True, exist_ok=True)
    with Image.open(source) as img:
        img = img.convert("RGB")
        for size in sizes:
            thumb = img.copy()
            if thumb.width > size:
                height = round(thumb.height * size / thumb.width)
                thumb = thumb.resize((size, height), Image.Resampling.LANCZOS)
            target = thumb_path(file_name, size)
            tmp = target.with_suffix(".tmp")
            thumb.save(tmp, "JPEG", quality=85, optimize=True, progressive=True)
            tmp.replace(target)


def delete_photo_files(file_name):
    for path in [photo_path(file_name)] + [
        thumb_path(file_name, size) for size in settings.THUMBNAIL_SIZES
    ]:
        path.unlink(missing_ok=True)


PREVIEW_SIDE = 2000


def preview_path(sha256):
    """Reduced copy of a whole scan, shown around a photo in the crop editor."""
    return Path(settings.PHOTOS_DIR) / "previews" / f"{sha256}.jpg"


def save_preview(image, sha256):
    height, width = image.shape[:2]
    scale = min(1.0, PREVIEW_SIDE / max(height, width))
    if scale < 1.0:
        size = (round(width * scale), round(height * scale))
        image = cv2.resize(image, size, interpolation=cv2.INTER_AREA)
    path = preview_path(sha256)
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise OSError(f"Could not encode the preview of {sha256[:12]}")
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(buffer.tobytes())
    tmp.replace(path)
    return path
