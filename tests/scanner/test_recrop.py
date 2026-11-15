"""Manual crop adjustment: geometry, worker application, reprocessing and views."""

import cv2
import numpy as np
import pytest
from django.urls import reverse

from catalog.models import Face, Person, Photo, Scan
from scanner.services import images, pipeline, recrop, rotation
from tests.scanner.test_pipeline import put_scan

ONE_PHOTO = [((620, 870), (600, 400), 0)]


def fake_photo(rotation=0, angle=4.0, inset=4, scan_size=(2000, 2000)):
    """An unsaved photo cut from a known rectangle (no files, no database)."""
    w, h = 600.0, 400.0
    width, height = int(w - 2 * inset), int(h - 2 * inset)
    if rotation % 180:
        width, height = height, width
    scan = Scan(width=scan_size[0], height=scan_size[1])
    return Photo(
        scan=scan,
        width=width,
        height=height,
        rotation=rotation,
        crop_rect={"cx": 1000.0, "cy": 900.0, "w": w, "h": h, "angle": angle},
    )


# --- Geometry -------------------------------------------------------------------------


@pytest.mark.parametrize("turn", [0, 90, 180, 270])
def test_unchanged_box_gives_the_same_rectangle(turn):
    photo = fake_photo(rotation=turn)
    rect = recrop.new_rect(photo, (0, 0, photo.width, photo.height), photo.crop_rect["angle"])
    assert rect == pytest.approx(photo.crop_rect, abs=0.11)


@pytest.mark.parametrize("turn", [0, 90, 180, 270])
@pytest.mark.parametrize("angle", [4.0, -3.5])
def test_box_corners_become_the_corners_of_the_new_photo(turn, angle):
    # What the editor shows at a corner of the box is what the worker puts at that corner.
    photo = fake_photo(rotation=turn)
    box = (-30, 12, photo.width + 25, photo.height - 40)
    rect = recrop.new_rect(photo, box, angle)
    margin = recrop.inset(photo)
    for (x, y), expected in [
        ((box[0], box[1]), (0, 0)),
        ((box[2], box[3]), (box[2] - box[0], box[3] - box[1])),
    ]:
        scan_point = recrop.view_to_scan(photo, x, y, angle)
        assert recrop.scan_to_view(rect, margin, turn, *scan_point) == pytest.approx(
            expected, abs=0.2
        )


@pytest.mark.parametrize("turn", [0, 90, 180, 270])
def test_cut_matches_the_view_mapping(turn):
    # Pixel content: the new photo shows, at each point, the scan point the editor drew.
    rng = np.random.default_rng(1)
    scan_image = cv2.GaussianBlur(rng.integers(0, 255, (1800, 2000, 3), dtype=np.uint8), (0, 0), 12)
    photo = fake_photo(rotation=turn, angle=5.0)
    rect = recrop.new_rect(photo, (0, 0, photo.width, photo.height), 5.0)
    image = recrop.cut(scan_image, rect, recrop.inset(photo), turn)
    assert image.shape[1] == pytest.approx(photo.width, abs=1)
    assert image.shape[0] == pytest.approx(photo.height, abs=1)
    for x, y in [(50, 60), (photo.width - 80, 40), (photo.width // 2, photo.height - 70)]:
        sx, sy = recrop.view_to_scan(photo, x, y)
        expected = scan_image[round(sy), round(sx)].astype(int)
        assert np.abs(image[y, x].astype(int) - expected).max() <= 6


def test_a_box_flush_with_the_scan_edge_is_accepted():
    # Regression: the trimmed inset may fall outside the scan; only what is shown counts.
    photo = fake_photo(angle=0.0)
    left_edge_on_scan = photo.crop_rect["cx"] - photo.crop_rect["w"] / 2 + recrop.inset(photo)
    photo.crop_rect["cx"] -= left_edge_on_scan  # the photo touches the left of the scan
    rect = recrop.new_rect(photo, (0, 0, photo.width, photo.height), 0.0)
    assert rect["cx"] - rect["w"] / 2 < 0  # the inset sticks out, and that is fine


def test_limits_are_enforced():
    photo = fake_photo()
    current = photo.crop_rect["angle"]
    with pytest.raises(recrop.CropError):
        recrop.new_rect(photo, (0, 0, 30, 30), current)  # too small
    with pytest.raises(recrop.CropError):
        recrop.new_rect(photo, (0, 0, photo.width, photo.height), current + 11)  # too rotated
    with pytest.raises(recrop.CropError):
        recrop.new_rect(photo, (-900, 0, photo.width, photo.height), current)  # off the scan


# --- Worker ---------------------------------------------------------------------------


@pytest.fixture
def scanned(db, data_dirs, settings):
    put_scan(settings.SCANS_INBOX_DIR, specs=ONE_PHOTO)
    (scan,) = pipeline.process_inbox()
    return scan.photos.get()


def test_processing_leaves_a_preview_and_the_scan_size(scanned):
    scan = scanned.scan
    assert (scan.width, scan.height) == (1240, 1754)
    assert images.preview_path(scan.sha256).exists()
    assert recrop.editor_ready(scanned)


def test_previews_are_made_for_older_scans(scanned):
    Scan.objects.filter(pk=scanned.scan_id).update(width=None, height=None)
    images.preview_path(scanned.scan.sha256).unlink()
    orphan = Scan.objects.create(sha256="f" * 64, file_name="gone.png")

    assert recrop.make_missing_previews() == 1

    scanned.scan.refresh_from_db()
    orphan.refresh_from_db()
    assert scanned.scan.width == 1240 and images.preview_path(scanned.scan.sha256).exists()
    assert orphan.width == 0  # original missing: not retried forever


def test_applying_a_crop_moves_faces_and_refreshes_the_photo(scanned, admin_user):
    maria = Person.objects.create(display_name="María")
    named = Face.objects.create(photo=scanned, x=200, y=100, w=60, h=60, person=maria)
    Face.objects.create(photo=scanned, x=400, y=150, w=50, h=50)  # automatic, untouched
    width, height = scanned.width, scanned.height
    version = scanned.image_version

    # Trim 30 px on the left and recover 40 px of the lid on the right.
    recrop.request_crop(
        admin_user, scanned, (30, 0, width + 40, height), scanned.crop_rect["angle"]
    )
    assert recrop.apply_pending_crops() == 1

    scanned.refresh_from_db()
    assert scanned.width == pytest.approx(width + 10, abs=2)
    assert scanned.height == pytest.approx(height, abs=2)
    assert scanned.crop_manual and scanned.pending_crop is None
    assert scanned.image_version == version + 1 and scanned.catalog_changed_at
    named.refresh_from_db()
    assert named.x == pytest.approx(170, abs=2) and named.y == pytest.approx(100, abs=2)
    assert named.embedding is not None  # recomputed on the new crop
    assert not scanned.faces.filter(person__isnull=True, source=Face.Source.AUTO).exists()
    image = cv2.imread(str(images.photo_path(scanned.file_name)))
    assert image.shape[1] == scanned.width
    assert image[:, -20:].mean() > 240  # the recovered strip is the white lid


def test_a_failing_crop_is_not_retried_forever(scanned, admin_user, settings):
    recrop.request_crop(
        admin_user, scanned, (0, 0, scanned.width, scanned.height), scanned.crop_rect["angle"]
    )
    (settings.SCANS_PROCESSED_DIR / scanned.scan.file_name).unlink()
    assert recrop.apply_pending_crops() == 0
    scanned.refresh_from_db()
    assert scanned.pending_crop is None and not scanned.crop_manual


def test_reprocessing_keeps_a_manual_crop(scanned, admin_user):
    recrop.request_crop(admin_user, scanned, (30, 20, scanned.width - 30, scanned.height - 20), 2.0)
    recrop.apply_pending_crops()
    scanned.refresh_from_db()
    rect, size = dict(scanned.crop_rect), (scanned.width, scanned.height)

    pipeline.reprocess(scanned.scan)

    (photo,) = scanned.scan.photos.all()
    assert photo.pk == scanned.pk and photo.crop_manual
    assert photo.crop_rect == rect
    assert (photo.width, photo.height) == pytest.approx(size, abs=1)


def test_rotation_bumps_the_image_version(scanned, admin_user):
    rotation.request_rotation(admin_user, scanned)
    rotation.apply_pending_rotations()
    scanned.refresh_from_db()
    assert scanned.image_version == 1


# --- Views ----------------------------------------------------------------------------


def test_crop_editor_is_for_admins_only(client, scanned, alice):
    client.force_login(alice)
    assert client.get(reverse("crop_photo", args=[scanned.pk])).status_code == 403
    assert client.post(reverse("crop_photo", args=[scanned.pk]), {}).status_code == 403
    assert client.get(reverse("scan_preview", args=[scanned.scan_id])).status_code == 403
    content = client.get(reverse("photo_detail", args=[scanned.pk])).content.decode()
    assert reverse("crop_photo", args=[scanned.pk]) not in content


def test_admin_opens_the_editor_and_saves_a_crop(client, scanned, admin_user):
    client.force_login(admin_user)
    content = client.get(reverse("photo_detail", args=[scanned.pk])).content.decode()
    assert reverse("crop_photo", args=[scanned.pk]) in content

    content = client.get(reverse("crop_photo", args=[scanned.pk])).content.decode()
    assert "data-crop-editor" in content and 'id="crop-data"' in content
    assert 'min="-10.0" max="10.0"' in content  # not "10,0": the Spanish locale breaks it
    assert client.get(reverse("scan_preview", args=[scanned.scan_id])).status_code == 200

    response = client.post(
        reverse("crop_photo", args=[scanned.pk]),
        {"x0": "10", "y0": "10", "x1": scanned.width - 10, "y1": scanned.height - 10, "angle": "1"},
    )
    assert response.status_code == 302
    scanned.refresh_from_db()
    assert scanned.pending_crop["angle"] == 1.0
    content = client.get(reverse("photo_detail", args=[scanned.pk])).content.decode()
    assert "Aplicando el recorte" in content and 'hx-trigger="every 3s"' in content


def test_invalid_crop_is_refused(client, scanned, admin_user):
    client.force_login(admin_user)
    response = client.post(
        reverse("crop_photo", args=[scanned.pk]),
        {"x0": "-5000", "y0": "0", "x1": "100", "y1": "100", "angle": "0"},
    )
    assert response.status_code == 400 and "se sale del escaneo" in response.content.decode()
    scanned.refresh_from_db()
    assert scanned.pending_crop is None


def test_editor_waits_for_the_preview(client, scanned, admin_user):
    images.preview_path(scanned.scan.sha256).unlink()
    client.force_login(admin_user)
    content = client.get(reverse("crop_photo", args=[scanned.pk])).content.decode()
    assert "data-crop-editor" not in content and "Vuelve a intentarlo en un minuto" in content
