import io
import os
import time

import cv2
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image

from accounts.models import AuditLog
from catalog.models import Photo, Scan, Upload
from scanner.services import faces, pipeline
from scanner.services.uploads import save_upload
from tests.conftest import FOUR_PHOTOS, synthetic_scan

pytestmark = pytest.mark.django_db


def image_file(name="foto.jpg", size=(640, 480), color=(120, 90, 60), fmt="JPEG"):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, fmt)
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/jpeg")


def scan_file(name="escaneo.png"):
    ok, data = cv2.imencode(".png", synthetic_scan(FOUR_PHOTOS))
    assert ok
    return SimpleUploadedFile(name, data.tobytes(), content_type="image/png")


def settle_inbox(settings):
    old = time.time() - 60
    for path in settings.SCANS_INBOX_DIR.iterdir():
        os.utime(path, (old, old))


def test_upload_requires_login(client, db):
    response = client.post(reverse("upload"), {"kind": "single", "files": [image_file()]})
    assert response.status_code == 302
    assert response.url.startswith(reverse("login"))


def test_upload_page_renders(client, alice, data_dirs):
    client.force_login(alice)
    content = client.get(reverse("upload")).content.decode()
    assert 'name="kind" value="scan"' in content and 'name="kind" value="single"' in content
    assert 'accept="image/jpeg,image/png,image/tiff"' in content


def test_upload_is_written_atomically_to_inbox(client, alice, data_dirs, settings):
    client.force_login(alice)
    response = client.post(
        reverse("upload"),
        {"kind": "single", "files": [image_file("Foto de la abuela.jpg")]},
        HTTP_ACCEPT="application/json",
    )

    assert response.json()["results"][0]["ok"] is True
    upload = Upload.objects.get()
    assert upload.user == alice and upload.kind == Upload.Kind.SINGLE
    assert upload.original_name == "Foto de la abuela.jpg"
    files = [p.name for p in settings.SCANS_INBOX_DIR.iterdir()]
    assert files == [upload.stored_name]  # no leftover .part file, safe generated name
    assert upload.stored_name.startswith("upload-") and upload.stored_name.endswith(".jpg")
    assert AuditLog.objects.filter(action="upload.create").exists()


def test_non_images_are_rejected(alice, data_dirs):
    fake = SimpleUploadedFile("virus.jpg", b"not an image at all", content_type="image/jpeg")
    result = save_upload(alice, fake, Upload.Kind.SCAN)
    assert not result.ok
    assert not Upload.objects.exists()


def test_too_big_files_are_rejected(alice, data_dirs, settings):
    settings.UPLOAD_MAX_MB = 0
    result = save_upload(alice, image_file(), Upload.Kind.SCAN)
    assert not result.ok


def test_invalid_kind_is_rejected(alice, data_dirs):
    assert not save_upload(alice, image_file(), "other").ok


def test_duplicates_are_rejected(alice, bob, data_dirs):
    assert save_upload(alice, image_file(), Upload.Kind.SINGLE).ok
    result = save_upload(bob, image_file(), Upload.Kind.SINGLE)
    assert not result.ok
    assert Upload.objects.count() == 1


def test_single_photo_is_used_whole(alice, data_dirs, settings, monkeypatch):
    monkeypatch.setattr(
        faces,
        "detect_faces",
        lambda image, model, threshold: [faces.DetectedFace(5, 5, 40, 40, 0.9)],
    )
    upload = save_upload(alice, image_file(size=(800, 600)), Upload.Kind.SINGLE).upload
    settle_inbox(settings)

    scan = pipeline.process_inbox()[0]

    photo = scan.photos.get()
    assert (photo.width, photo.height) == (800, 600)
    assert photo.faces.count() == 1
    assert scan.uploaded_by == alice
    upload.refresh_from_db()
    assert upload.status == Upload.Status.PROCESSED and upload.scan == scan


def test_uploaded_scan_is_cropped(alice, data_dirs, settings):
    upload = save_upload(alice, scan_file(), Upload.Kind.SCAN).upload
    settle_inbox(settings)

    scan = pipeline.process_inbox()[0]

    assert scan.photos.count() == 4
    upload.refresh_from_db()
    assert upload.status == Upload.Status.PROCESSED


def test_failed_processing_marks_the_upload(alice, data_dirs, settings, monkeypatch):
    upload = save_upload(alice, image_file(), Upload.Kind.SINGLE).upload
    settle_inbox(settings)
    monkeypatch.setattr(pipeline, "analyse", lambda *args, **kwargs: 1 / 0)

    pipeline.process_inbox(skip=set())

    upload.refresh_from_db()
    assert upload.status == Upload.Status.FAILED
    assert not Scan.objects.exists()


def test_reprocessing_keeps_single_photos_whole(alice, data_dirs, settings):
    save_upload(alice, image_file(size=(800, 600)), Upload.Kind.SINGLE)
    settle_inbox(settings)
    scan = pipeline.process_inbox()[0]

    pipeline.reprocess(scan)

    assert list(Photo.objects.filter(scan=scan).values_list("width", "height")) == [(800, 600)]


def test_my_uploads_are_listed(client, alice, bob, data_dirs):
    save_upload(alice, image_file(color=(1, 2, 3)), Upload.Kind.SINGLE)
    save_upload(bob, image_file(color=(9, 9, 9)), Upload.Kind.SINGLE)
    client.force_login(alice)
    response = client.get(reverse("upload"))
    assert [u.user for u in response.context["my_uploads"]] == [alice]
