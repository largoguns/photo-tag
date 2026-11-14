import numpy as np
import pytest
from django.core.management import call_command

from catalog.models import Face
from scanner.services import embeddings, faces, pipeline
from tests.scanner.test_pipeline import put_scan

pytestmark = pytest.mark.django_db


def test_embedding_is_normalized_128_vector_without_landmarks():
    image = np.random.default_rng(0).integers(0, 255, (600, 800, 3), dtype=np.uint8)

    vector = embeddings.compute_embedding(image, (100, 100, 80, 100))

    assert vector.shape == (128,)
    assert np.linalg.norm(vector) == pytest.approx(1, abs=1e-4)


def test_embedding_roundtrip_bytes():
    vector = np.arange(128, dtype=np.float32)
    assert np.array_equal(embeddings.from_bytes(embeddings.to_bytes(vector)), vector)


def test_compute_missing_fills_only_missing(photo):
    first, second = photo.faces.all()
    Face.objects.filter(pk=second.pk).update(embedding=b"\0" * 512)

    assert embeddings.compute_missing() == 1

    first.refresh_from_db()
    assert len(first.embedding) == 512
    assert embeddings.compute_missing() == 0


def test_removed_faces_are_skipped(photo):
    Face.objects.filter(photo=photo).update(status=Face.Status.REMOVED)
    assert embeddings.compute_missing() == 0


def test_compute_embeddings_command_recomputes_all(photo):
    Face.objects.update(embedding=b"\0" * 512)
    call_command("compute_embeddings", "--all")
    assert not Face.objects.filter(embedding=b"\0" * 512).exists()


@pytest.fixture
def fake_faces(monkeypatch):
    monkeypatch.setattr(
        faces,
        "detect_faces",
        lambda image, model, threshold: [faces.DetectedFace(30, 30, 60, 60, 0.95)],
    )


def test_scanning_stores_embeddings(data_dirs, settings, fake_faces):
    put_scan(settings.SCANS_INBOX_DIR)
    pipeline.process_inbox()

    assert Face.objects.count() == 4
    assert not Face.objects.filter(embedding__isnull=True).exists()


def test_reprocessing_recomputes_kept_face_embeddings(data_dirs, settings, fake_faces):
    put_scan(settings.SCANS_INBOX_DIR)
    scan = pipeline.process_inbox()[0]
    face = Face.objects.first()
    Face.objects.filter(pk=face.pk).update(source=Face.Source.MANUAL, embedding=b"\0" * 512)

    pipeline.reprocess(scan)

    face.refresh_from_db()
    assert face.embedding != b"\0" * 512
    assert not Face.objects.filter(embedding__isnull=True).exists()
