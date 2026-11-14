import os
import time

import cv2
import pytest
from django.core.management import call_command

from catalog.models import Face, Photo, Proposal, Scan
from catalog.services import voting
from scanner.services import faces, pipeline
from tests.conftest import FOUR_PHOTOS, synthetic_scan

pytestmark = pytest.mark.django_db


def put_scan(directory, name="scan01.png", specs=FOUR_PHOTOS, seed=0):
    path = directory / name
    cv2.imwrite(str(path), synthetic_scan(specs, seed=seed))
    old = time.time() - 60  # settled: not being copied any more
    os.utime(path, (old, old))
    return path


def test_process_inbox_creates_photos_thumbnails_and_moves_original(data_dirs, settings):
    put_scan(settings.SCANS_INBOX_DIR)

    scans = pipeline.process_inbox()

    assert len(scans) == 1
    scan = scans[0]
    assert scan.photos.count() == 4
    assert not scan.needs_review
    for photo in scan.photos.all():
        assert (settings.PHOTOS_DIR / photo.file_name).exists()
        for size in (400, 1600):
            assert (settings.THUMBS_DIR / f"{photo.file_name[:-4]}_{size}.jpg").exists()
    assert not list(settings.SCANS_INBOX_DIR.iterdir())
    assert (settings.SCANS_PROCESSED_DIR / "scan01.png").exists()
    assert scan.file_name == "scan01.png"


def test_processing_is_idempotent_by_hash(data_dirs, settings):
    put_scan(settings.SCANS_INBOX_DIR)
    pipeline.process_inbox()
    put_scan(settings.SCANS_INBOX_DIR, name="copy-of-scan01.png")

    assert pipeline.process_inbox() == []
    assert Scan.objects.count() == 1
    assert Photo.objects.count() == 4
    # The duplicate is kept (moved, never deleted).
    assert (settings.SCANS_PROCESSED_DIR / "copy-of-scan01.png").exists()


def test_unsettled_files_are_left_for_next_poll(data_dirs, settings):
    path = put_scan(settings.SCANS_INBOX_DIR)
    os.utime(path, None)  # just modified

    assert pipeline.process_inbox() == []
    assert path.exists()


def test_detected_faces_are_stored_as_auto(data_dirs, settings, monkeypatch):
    monkeypatch.setattr(
        faces,
        "detect_faces",
        lambda image, model, threshold: [faces.DetectedFace(10, 20, 30, 40, 0.95)],
    )
    put_scan(settings.SCANS_INBOX_DIR)

    pipeline.process_inbox()

    assert Face.objects.count() == 4
    face = Face.objects.first()
    assert (face.x, face.y, face.w, face.h, face.source) == (10, 20, 30, 40, Face.Source.AUTO)


def test_needs_review_flag(data_dirs, settings):
    put_scan(settings.SCANS_INBOX_DIR, specs=[])

    scan = pipeline.process_inbox()[0]

    assert scan.needs_review
    assert scan.photos.count() == 0


def test_force_reprocess_keeps_votes_of_existing_photos(data_dirs, settings, alice, boda):
    put_scan(settings.SCANS_INBOX_DIR)
    scan = pipeline.process_inbox()[0]
    photo = scan.photos.get(index=0)
    proposal = voting.create_proposal(alice, photo, Proposal.Kind.EVENT, event_type=boda)

    call_command("process_scans", force=scan.sha256[:10])

    scan.refresh_from_db()
    assert scan.photos.count() == 4
    assert Photo.objects.filter(pk=photo.pk).exists()
    proposal.refresh_from_db()
    assert proposal.votes.count() == 1


def test_force_reprocess_by_file_name(data_dirs, settings):
    put_scan(settings.SCANS_INBOX_DIR)
    scan = pipeline.process_inbox()[0]

    assert pipeline.find_scan("scan01.png") == scan
    pipeline.reprocess(scan)
    assert scan.photos.count() == 4


def test_yunet_runs_on_blank_image(settings):
    import numpy as np

    found = faces.detect_faces(np.zeros((900, 1600, 3), np.uint8), settings.YUNET_MODEL_PATH)
    assert found == []
