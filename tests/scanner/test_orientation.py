import cv2
import numpy as np
import pytest
from django.urls import reverse

from accounts.models import AuditLog
from catalog.models import Photo, Proposal
from catalog.services import voting
from scanner.services import faces, images, orientation, pipeline
from scanner.services.rotation import apply_pending_rotations, request_rotation

pytestmark = pytest.mark.django_db


def marked_image(width=400, height=300):
    """An image whose 'up' is known: a red block in the top-left corner."""
    image = np.full((height, width, 3), 120, np.uint8)
    image[: height // 4, : width // 4] = (0, 0, 255)
    return image


def upright(image):
    small = image[: image.shape[0] // 8, : image.shape[1] // 8]
    return small.mean(axis=(0, 1))[2] > 200


@pytest.fixture
def fake_detector(monkeypatch):
    """Detector that only 'sees' a face when the image is upright."""

    def detect(image, model, threshold):
        return [faces.DetectedFace(10, 10, 30, 30, 0.95)] if upright(image) else []

    monkeypatch.setattr(faces, "detect_faces", detect)


@pytest.mark.parametrize("placed", [90, 180, 270])
def test_detects_the_rotation_that_makes_faces_upright(fake_detector, placed):
    # The photo was placed rotated by `placed`; undoing it needs (360 - placed).
    sideways = orientation.rotate(marked_image(), placed)
    found = orientation.detect_rotation(sideways)
    assert found.degrees == (360 - placed) % 360
    assert not found.uncertain
    assert upright(orientation.rotate(sideways, found.degrees))


def test_upright_photos_and_photos_without_faces_are_untouched(fake_detector, monkeypatch):
    assert orientation.detect_rotation(marked_image()).degrees == 0
    monkeypatch.setattr(faces, "detect_faces", lambda image, model, threshold: [])
    assert orientation.detect_rotation(orientation.rotate(marked_image(), 90)).degrees == 0


def test_ambiguous_orientation_is_not_rotated_but_flagged(monkeypatch):
    calls = iter(
        [[], [faces.DetectedFace(0, 0, 9, 9, 0.9)], [faces.DetectedFace(0, 0, 9, 9, 0.8)], []]
    )
    monkeypatch.setattr(faces, "detect_faces", lambda image, model, threshold: next(calls))
    found = orientation.detect_rotation(marked_image())
    assert found.degrees == 0 and found.uncertain


@pytest.mark.parametrize("degrees", [90, 180, 270])
def test_rotate_box_matches_pixel_rotation(degrees):
    width, height, box = 400, 300, (50, 20, 60, 90)
    image = np.zeros((height, width), np.uint8)
    x, y, w, h = box
    image[y : y + h, x : x + w] = 255
    ys, xs = np.nonzero(orientation.rotate(image, degrees))
    pixels = (xs.min(), ys.min(), xs.max() - xs.min() + 1, ys.max() - ys.min() + 1)
    assert orientation.rotate_box(*box, width, height, degrees) == pixels


def test_single_upload_placed_sideways_is_fixed(fake_detector, alice, data_dirs, settings):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from scanner.services.uploads import save_upload
    from tests.scanner.test_uploads import settle_inbox

    ok, data = cv2.imencode(".png", orientation.rotate(marked_image(), 90))
    save_upload(alice, SimpleUploadedFile("lado.png", data.tobytes()), "single")
    settle_inbox(settings)

    photo = pipeline.process_inbox()[0].photos.get()

    assert photo.rotation == 270
    assert (photo.width, photo.height) == (400, 300)
    assert upright(cv2.imread(str(images.photo_path(photo.file_name))))


# --- Manual rotation -------------------------------------------------------------------


def test_rotate_button_queues_and_worker_applies(client, photo, alice, monkeypatch):
    voting.create_proposal(
        alice, photo, Proposal.Kind.ADD_FACE, value={"x": 600, "y": 400, "w": 50, "h": 60}
    )
    face = photo.faces.order_by("pk").first()  # (100, 100, 80, 100) on 800x600
    client.force_login(alice)

    client.post(reverse("rotate_photo", args=[photo.pk]))
    client.post(reverse("rotate_photo", args=[photo.pk]), HTTP_HX_REQUEST="true")
    photo.refresh_from_db()
    assert photo.pending_rotation == 180
    assert AuditLog.objects.filter(action="photo.rotate").count() == 2
    content = client.get(reverse("photo_detail", args=[photo.pk])).content.decode()
    assert "Aplicando el giro" in content and 'hx-trigger="every 3s"' in content

    assert apply_pending_rotations() == 1

    photo.refresh_from_db()
    face.refresh_from_db()
    assert (photo.rotation, photo.pending_rotation) == (180, 0)
    assert (face.x, face.y, face.w, face.h) == (800 - 180, 600 - 200, 80, 100)
    assert face.embedding is None
    added = Proposal.objects.get(kind=Proposal.Kind.ADD_FACE)
    assert (added.value["x"], added.value["y"]) == (800 - 650, 600 - 460)
    assert photo.catalog_changed_at is not None
    assert (
        "Aplicando el giro"
        not in client.get(reverse("photo_detail", args=[photo.pk])).content.decode()
    )


def test_quarter_turn_swaps_dimensions_and_file(photo, alice):
    request_rotation(alice, photo)
    apply_pending_rotations()
    photo.refresh_from_db()
    assert (photo.width, photo.height) == (600, 800)
    stored = cv2.imread(str(images.photo_path(photo.file_name)))
    assert stored.shape[:2] == (800, 600)


def test_four_turns_do_nothing(photo, alice):
    for _ in range(4):
        request_rotation(alice, photo)
    assert apply_pending_rotations() == 0
    photo.refresh_from_db()
    assert (photo.pending_rotation, photo.rotation) == (0, 0)


def test_rotate_requires_login(client, photo):
    assert client.post(reverse("rotate_photo", args=[photo.pk])).status_code == 302
    assert Photo.objects.get(pk=photo.pk).pending_rotation == 0


def test_reprocess_keeps_manual_rotation(data_dirs, settings, alice):
    from tests.scanner.test_pipeline import put_scan

    put_scan(settings.SCANS_INBOX_DIR)
    scan = pipeline.process_inbox()[0]
    photo = scan.photos.get(index=0)
    width, height = photo.width, photo.height
    request_rotation(alice, photo)
    apply_pending_rotations()

    pipeline.reprocess(scan)

    photo.refresh_from_db()
    assert photo.rotation == 90
    assert (photo.width, photo.height) == (height, width)
    stored = cv2.imread(str(images.photo_path(photo.file_name)))
    assert stored.shape[:2] == (width, height)
