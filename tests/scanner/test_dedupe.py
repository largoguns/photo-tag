import cv2
import numpy as np
import pytest
from django.core.management import call_command
from django.urls import reverse

from catalog.models import Photo, Scan
from scanner.services import dedupe, images, orientation, pipeline

pytestmark = pytest.mark.django_db


def picture(seed=0, size=(480, 640)):
    """A photo-like image: smooth gradients and a few blobs."""
    rng = np.random.default_rng(seed)
    h, w = size
    yy, xx = np.mgrid[0:h, 0:w]
    image = np.stack(
        [(xx * rng.uniform(0.1, 0.4) + yy * rng.uniform(0.1, 0.4)) % 255 for _ in range(3)], axis=2
    ).astype(np.uint8)
    for _ in range(6):
        center = (int(rng.integers(0, w)), int(rng.integers(0, h)))
        cv2.circle(
            image, center, int(rng.integers(20, 90)), [int(c) for c in rng.integers(0, 255, 3)], -1
        )
    return image


def recompress(image, quality=40):
    ok, data = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def distance(a, b):
    return min(dedupe.hamming(v, dedupe.dhash(b)) for v in dedupe.hash_variants(a))


def test_copies_are_close_and_different_photos_are_not():
    original = picture(1)
    assert distance(original, recompress(original)) <= 6
    assert distance(original, cv2.resize(original, (320, 240))) <= 6
    assert distance(original, orientation.rotate(original, 90)) <= 6
    assert distance(original, cv2.convertScaleAbs(original, alpha=1.1, beta=10)) <= 6
    assert distance(original, picture(2)) > 6


def test_flat_images_have_no_hash():
    assert dedupe.dhash(np.full((100, 100, 3), 128, np.uint8)) == ""


def make_photo(index, image, scan=None):
    scan = scan or Scan.objects.create(sha256=f"{index}".ljust(64, "d"), file_name=f"s{index}.jpg")
    name = f"dup-{index}.jpg"
    images.save_photo(image, name)
    return Photo.objects.create(
        scan=scan, file_name=name, index=0, width=image.shape[1], height=image.shape[0]
    )


def test_pipeline_flags_a_rescan_for_review(data_dirs, settings):
    original = picture(3)
    first = make_photo(1, original)
    Photo.objects.filter(pk=first.pk).update(phash=dedupe.dhash(original))

    scan = Scan.objects.create(sha256="f" * 64, file_name="rescan.jpg")
    copy = make_photo(2, recompress(original), scan=scan)
    duplicate = pipeline._fingerprint(copy, recompress(original))
    pipeline._flag_duplicates(scan, [duplicate])

    copy.refresh_from_db()
    scan.refresh_from_db()
    assert copy.duplicate_of == first
    assert scan.needs_review and {"code": "duplicate", "photo": first.pk} in scan.review_reasons
    assert f"Posible duplicado de la foto {first.pk}" in scan.review_reason_labels()


def test_review_queue_shows_pair_and_not_duplicate_clears_it(client, admin_user, data_dirs):
    original = picture(4)
    first = make_photo(1, original)
    copy = make_photo(2, recompress(original))
    Photo.objects.filter(pk=copy.pk).update(duplicate_of=first)
    Scan.objects.filter(pk=copy.scan_id).update(needs_review=True)
    client.force_login(admin_user)

    content = client.get(reverse("admin_review_queue")).content.decode()
    assert "No es duplicado" in content
    assert reverse("photo_image", args=[first.pk, 400]) in content

    client.post(reverse("admin_photo_not_duplicate", args=[copy.pk]))
    copy.refresh_from_db()
    assert copy.duplicate_of is None


def test_photo_page_warns_about_duplicate(client, alice, data_dirs):
    original = picture(5)
    first = make_photo(1, original)
    copy = make_photo(2, original)
    Photo.objects.filter(pk=copy.pk).update(duplicate_of=first)
    client.force_login(alice)
    content = client.get(reverse("photo_detail", args=[copy.pk])).content.decode()
    assert (
        "Posible duplicado de la" in content and reverse("photo_detail", args=[first.pk]) in content
    )


def test_find_duplicates_command_flags_existing_photos(data_dirs):
    original = picture(6)
    first = make_photo(1, original)
    copy = make_photo(2, recompress(original, 30))
    other = make_photo(3, picture(7))

    call_command("find_duplicates")

    copy.refresh_from_db()
    first.refresh_from_db()
    other.refresh_from_db()
    assert copy.duplicate_of == first
    assert first.duplicate_of is None and other.duplicate_of is None
    assert first.phash and other.phash
    assert Scan.objects.get(pk=copy.scan_id).needs_review
