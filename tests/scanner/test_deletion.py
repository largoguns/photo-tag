"""Deleting photos and scans, and reprocessing scans from the web."""

import os
import time

import cv2
import numpy as np
import pytest
from django.urls import reverse

from accounts.models import AuditLog
from catalog.models import Face, FileCleanup, Person, Photo, Proposal, Scan, Upload, Vote
from catalog.services import voting
from scanner.services import cropping, deletion, images, pipeline
from tests.conftest import synthetic_scan
from tests.scanner.test_cropping import _textured
from tests.scanner.test_pipeline import put_scan

pytestmark = pytest.mark.django_db


@pytest.fixture
def scan(data_dirs, settings):
    put_scan(settings.SCANS_INBOX_DIR)
    (scan,) = pipeline.process_inbox()
    return scan


def test_deleting_a_photo_removes_it_with_its_votes_and_files(scan, alice, settings):
    photo = scan.photos.order_by("index").first()
    face = Face.objects.create(photo=photo, x=10, y=10, w=40, h=40)
    proposal = voting.create_proposal(
        alice, photo, Proposal.Kind.FACE_IDENTITY, face=face, person_name="Nadie Más"
    )
    provisional = Person.objects.get(display_name="Nadie Más")
    assert provisional.is_provisional and Vote.objects.filter(proposal=proposal).exists()
    exported = settings.EXPORT_DIR / "1985" / photo.file_name
    exported.parent.mkdir(parents=True)
    exported.write_bytes(b"jpg")
    exported.with_name(f"{photo.file_name}.xmp").write_bytes(b"xmp")
    Photo.objects.filter(pk=photo.pk).update(export_path=f"1985/{photo.file_name}")
    photo.refresh_from_db()

    deletion.delete_photo(alice, photo)

    assert not Photo.objects.filter(pk=photo.pk).exists()
    assert not Proposal.objects.exists() and not Vote.objects.exists()
    assert not Person.objects.filter(pk=provisional.pk).exists()
    assert AuditLog.objects.filter(action="photo.delete").exists()
    assert set(FileCleanup.objects.values_list("kind", flat=True)) == {"photo", "export"}
    assert images.photo_path(photo.file_name).exists()  # files go with the worker

    assert deletion.run_cleanups() == 2
    assert not images.photo_path(photo.file_name).exists()
    assert not images.thumb_path(photo.file_name, 400).exists()
    assert not exported.exists() and not exported.with_name(f"{photo.file_name}.xmp").exists()
    assert not FileCleanup.objects.exists()
    assert scan.photos.count() == 3


def test_deleting_a_scan_sets_the_original_aside_and_allows_processing_it_again(
    scan, alice, settings
):
    upload = Upload.objects.create(
        user=alice,
        original_name="a.png",
        stored_name="a.png",
        kind=Upload.Kind.SCAN,
        sha256=scan.sha256,
        size=1,
        scan=scan,
    )
    names = list(scan.photos.values_list("file_name", flat=True))

    deletion.delete_scan(alice, scan)
    deletion.run_cleanups()

    assert not Scan.objects.exists() and not Photo.objects.exists()
    upload.refresh_from_db()
    assert upload.scan is None
    assert (settings.SCANS_DELETED_DIR / "scan01.png").exists()
    assert not (settings.SCANS_PROCESSED_DIR / "scan01.png").exists()
    assert not images.preview_path(scan.sha256).exists()
    assert not any(images.photo_path(name).exists() for name in names)

    # The same original, put back in the inbox, is processed as new.
    original = settings.SCANS_DELETED_DIR / "scan01.png"
    target = settings.SCANS_INBOX_DIR / "scan01.png"
    original.rename(target)
    old = time.time() - 60
    os.utime(target, (old, old))
    assert len(pipeline.process_inbox()) == 1


def touching_scan():
    """Two prints stacked with no lid between them, as in the real scan of 2026-11-15."""
    rng = np.random.default_rng(3)
    page = synthetic_scan([])
    page[200:800, 300:1000] = _textured(rng, (700, 600), (60, 90, 140))
    page[800:1400, 300:1000] = _textured(rng, (700, 600), (150, 120, 70))
    page[797:803, 300:1000] = 50
    return page


def test_reprocessing_from_the_web_applies_the_current_cropping(
    data_dirs, settings, alice, monkeypatch
):
    path = settings.SCANS_INBOX_DIR / "touching.png"
    cv2.imwrite(str(path), touching_scan())
    old = time.time() - 60
    os.utime(path, (old, old))
    with monkeypatch.context() as patch:
        patch.setattr(cropping, "SPLIT_ASPECT", 99)  # as before seams were detected
        (scan,) = pipeline.process_inbox()
    assert scan.photos.count() == 1

    deletion.request_reprocess(alice, scan)
    assert deletion.run_reprocess_requests() == 1

    scan.refresh_from_db()
    assert scan.photos.count() == 2 and not scan.reprocess_requested


def test_reprocess_without_the_original_does_not_get_stuck(scan, alice, settings):
    (settings.SCANS_PROCESSED_DIR / "scan01.png").unlink()
    deletion.request_reprocess(alice, scan)
    assert deletion.run_reprocess_requests() == 0
    scan.refresh_from_db()
    assert not scan.reprocess_requested


# --- Views ----------------------------------------------------------------------------


def test_collaborators_cannot_delete_or_reprocess(client, scan, alice):
    photo = scan.photos.first()
    client.force_login(alice)
    assert client.post(reverse("admin_photo_delete", args=[photo.pk])).status_code == 403
    assert client.post(reverse("admin_scan_delete", args=[scan.pk])).status_code == 403
    assert client.post(reverse("admin_scan_reprocess", args=[scan.pk])).status_code == 403
    content = client.get(reverse("photo_detail", args=[photo.pk])).content.decode()
    assert reverse("admin_photo_delete", args=[photo.pk]) not in content


def test_admin_deletes_a_photo_and_goes_to_the_next_one(client, scan, admin_user):
    first, second = scan.photos.order_by("index")[:2]
    client.force_login(admin_user)
    content = client.get(reverse("photo_detail", args=[first.pk])).content.decode()
    assert reverse("admin_photo_delete", args=[first.pk]) in content
    assert "Escaneo de origen" in content

    response = client.post(reverse("admin_photo_delete", args=[first.pk]))
    assert response.url == reverse("photo_detail", args=[second.pk])

    response = client.post(
        reverse("admin_photo_delete", args=[second.pk]), {"next": "https://evil.example/"}
    )
    assert "evil" not in response.url


def test_admin_reprocesses_from_the_photo(client, scan, admin_user):
    photo = scan.photos.first()
    client.force_login(admin_user)
    url = reverse("photo_detail", args=[photo.pk])
    response = client.post(reverse("admin_scan_reprocess", args=[scan.pk]), {"next": url})
    assert response.url == url
    content = client.get(url).content.decode()
    assert "Reprocesando el escaneo" in content and 'hx-trigger="every 3s"' in content


def test_admin_deletes_a_scan_from_the_review_queue(client, scan, admin_user):
    Scan.objects.filter(pk=scan.pk).update(needs_review=True)
    client.force_login(admin_user)
    content = client.get(reverse("admin_review_queue")).content.decode()
    assert reverse("admin_scan_delete", args=[scan.pk]) in content
    assert reverse("admin_scan_reprocess", args=[scan.pk]) in content

    response = client.post(
        reverse("admin_scan_delete", args=[scan.pk]), {"next": reverse("admin_review_queue")}
    )
    assert response.url == reverse("admin_review_queue")
    assert not Scan.objects.exists()
