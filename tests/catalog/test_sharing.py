import io
import zipfile

import numpy as np
import pytest
from django.urls import reverse

from accounts.models import AuditLog
from catalog.models import Photo
from scanner.services import images

pytestmark = pytest.mark.django_db


@pytest.fixture
def photos(photo, data_dirs):
    """Three stored photos (the fixture one plus two more)."""
    result = [photo]
    for index in (1, 2):
        name = f"share-{index}.jpg"
        images.save_photo(np.full((60, 80, 3), 40 * index, np.uint8), name)
        result.append(
            Photo.objects.create(scan=photo.scan, file_name=name, index=index, width=80, height=60)
        )
    return result


def test_download_requires_login(client, photos):
    response = client.get(reverse("download_photos"), {"ids": photos[0].pk})
    assert response.status_code == 302


def test_single_photo_downloads_as_jpeg(client, alice, photos):
    client.force_login(alice)
    response = client.get(reverse("download_photos"), {"ids": photos[0].pk})
    assert response["Content-Type"] == "image/jpeg"
    assert f"PhotoTag-sin-fecha-{photos[0].pk}.jpg" in response["Content-Disposition"]
    assert AuditLog.objects.filter(action="photo.download").exists()


def test_several_photos_download_as_zip(client, alice, photos):
    client.force_login(alice)
    ids = ",".join(str(p.pk) for p in photos)
    response = client.get(reverse("download_photos"), {"ids": ids})
    assert response["Content-Type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(b"".join(response.streaming_content)))
    assert archive.namelist() == [f"PhotoTag-sin-fecha-{p.pk}.jpg" for p in photos]
    assert (
        archive.read(archive.namelist()[1]) == images.photo_path(photos[1].file_name).read_bytes()
    )


def test_download_skips_discarded_and_respects_limit(client, alice, photos, settings):
    Photo.objects.filter(pk=photos[1].pk).update(discarded=True)
    settings.SHARE_MAX_PHOTOS = 1
    client.force_login(alice)
    response = client.get(
        reverse("download_photos"), {"ids": f"{photos[1].pk},{photos[2].pk},{photos[0].pk}"}
    )
    # The discarded photo is skipped; the limit (1) keeps only the next valid one.
    assert response["Content-Type"] == "image/jpeg"
    assert f"PhotoTag-sin-fecha-{photos[2].pk}.jpg" in response["Content-Disposition"]


def test_unknown_ids_give_404(client, alice, db):
    client.force_login(alice)
    assert client.get(reverse("download_photos"), {"ids": "999,abc"}).status_code == 404


def test_share_is_audited(client, alice, photos):
    client.force_login(alice)
    response = client.post(reverse("log_share"), {"ids": f"{photos[0].pk},{photos[2].pk}"})
    assert response.status_code == 204
    entry = AuditLog.objects.get(action="photo.share")
    assert entry.details["count"] == 2


def test_share_buttons_and_selection_mode_are_rendered(client, alice, photos):
    client.force_login(alice)
    page = client.get(reverse("photo_detail", args=[photos[0].pk])).content.decode()
    assert f'data-share-photos="{photos[0].pk}"' in page

    gallery = client.get(reverse("gallery")).content.decode()
    assert 'data-selection="start"' in gallery and 'id="selection-bar"' in gallery
    assert f'data-photo-id="{photos[0].pk}"' in gallery
    assert "data-gallery-zoom" in gallery
