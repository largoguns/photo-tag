"""Download/share several photos: a ZIP streamed with the standard library."""

import io
import zipfile

from django.conf import settings
from django.utils.translation import gettext

from catalog.models import Photo
from scanner.services.images import photo_path

MAX_REQUESTED_IDS = 500


def parse_ids(raw):
    """'3,1,3,x' → [3, 1] (unique, in order)."""
    ids = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if part.isdigit() and int(part) not in ids:
            ids.append(int(part))
    return ids[:MAX_REQUESTED_IDS]


def photos_for(ids):
    """Visible photos with their file on disk, in the requested order, at most SHARE_MAX_PHOTOS."""
    found = {p.pk: p for p in Photo.objects.visible().filter(pk__in=ids)}
    photos = [found[pk] for pk in ids if pk in found and photo_path(found[pk].file_name).exists()]
    return photos[: settings.SHARE_MAX_PHOTOS]


def download_name(photo):
    """Readable file name: PhotoTag-1985-123.jpg (or with "sin-fecha")."""
    year = photo.date_value.year if photo.date_value else gettext("sin-fecha")
    return f"PhotoTag-{year}-{photo.pk}.jpg"


class _Sink(io.RawIOBase):
    """Write-only buffer that hands out what has been written so far."""

    def __init__(self):
        self.chunks = []

    def writable(self):
        return True

    def write(self, data):
        self.chunks.append(bytes(data))
        return len(data)

    def drain(self):
        chunks, self.chunks = self.chunks, []
        return b"".join(chunks)


def zip_stream(photos, chunk_size=1024 * 1024):
    """Yield a ZIP of the photos piece by piece (JPEGs are stored, not recompressed)."""
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED) as archive:
        for photo in photos:
            with (
                photo_path(photo.file_name).open("rb") as source,
                archive.open(download_name(photo), "w", force_zip64=True) as target,
            ):
                while chunk := source.read(chunk_size):
                    target.write(chunk)
                    yield sink.drain()
    yield sink.drain()
