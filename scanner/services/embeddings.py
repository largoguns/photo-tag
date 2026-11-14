"""Face feature vectors with OpenCV SFace, used only to suggest identities (spec section 3.5)."""

import logging
from functools import lru_cache
from itertools import groupby

import cv2
import numpy as np
from django.conf import settings

from catalog.models import Face
from scanner.services import faces
from scanner.services.images import photo_path

logger = logging.getLogger("scanner")

EMBEDDING_DIM = 128
SFACE_INPUT = 112
CROP_MARGIN = 0.5
# Size the face crop is scaled to before re-running YuNet for landmarks.
LANDMARK_CROP_SIDE = 320
LANDMARK_SCORE_THRESHOLD = 0.5
MIN_LANDMARK_IOU = 0.2


@lru_cache(maxsize=2)
def _recognizer(model_path):
    return cv2.FaceRecognizerSF.create(str(model_path), "")


def to_bytes(vector):
    return np.asarray(vector, dtype=np.float32).tobytes()


def from_bytes(data):
    return np.frombuffer(bytes(data), dtype=np.float32)


def _normalize(vector):
    vector = np.asarray(vector, dtype=np.float32).ravel()
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else vector


def _iou(a, b):
    ax1, ay1, bx1, by1 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw = max(0.0, min(ax1, bx1) - max(a[0], b[0]))
    ih = max(0.0, min(ay1, by1) - max(a[1], b[1]))
    inter = iw * ih
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union else 0.0


def _aligned_face(image, box):
    """112x112 face aligned with YuNet landmarks, or a plain resize if none are found."""
    height, width = image.shape[:2]
    x, y, w, h = box
    margin = CROP_MARGIN * max(w, h)
    x0, y0 = max(0, int(x - margin)), max(0, int(y - margin))
    x1, y1 = min(width, int(x + w + margin)), min(height, int(y + h + margin))
    crop = image[y0:y1, x0:x1]
    if crop.size == 0:
        raise ValueError("Empty face crop")

    scale = LANDMARK_CROP_SIDE / max(crop.shape[:2])
    scaled = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    target = ((x - x0) * scale, (y - y0) * scale, w * scale, h * scale)

    detector = faces._detector(str(settings.YUNET_MODEL_PATH), LANDMARK_SCORE_THRESHOLD)
    detector.setInputSize((scaled.shape[1], scaled.shape[0]))
    _, detections = detector.detect(scaled)
    if detections is not None:
        best = max(detections, key=lambda row: _iou(row[:4], target))
        if _iou(best[:4], target) >= MIN_LANDMARK_IOU:
            recognizer = _recognizer(str(settings.SFACE_MODEL_PATH))
            return recognizer.alignCrop(scaled, best)

    fx, fy = int(x - x0), int(y - y0)
    return cv2.resize(crop[fy : fy + h, fx : fx + w], (SFACE_INPUT, SFACE_INPUT))


def compute_embedding(image, box):
    """Normalized 128-d feature vector of the face at `box` (x, y, w, h) in a BGR image."""
    recognizer = _recognizer(str(settings.SFACE_MODEL_PATH))
    return _normalize(recognizer.feature(_aligned_face(image, box)))


def embed_faces(image, face_objects):
    """Compute and store the embedding of each face of one photo. Returns how many."""
    done = 0
    for face in face_objects:
        try:
            face.embedding = to_bytes(compute_embedding(image, (face.x, face.y, face.w, face.h)))
        except Exception:
            logger.exception("event=embedding_failed face=%s", face.pk)
            continue
        Face.objects.filter(pk=face.pk).update(embedding=face.embedding)
        done += 1
    return done


def compute_missing(limit=500):
    """Fill the embeddings that are still missing (manual faces, backfill). Returns how many."""
    pending = list(
        Face.objects.filter(
            embedding__isnull=True,
            status__in=[Face.Status.ACTIVE, Face.Status.PROPOSED],
            photo__discarded=False,
        )
        .select_related("photo")
        .order_by("photo_id", "id")[:limit]
    )
    done = 0
    for _, group in groupby(pending, key=lambda f: f.photo_id):
        group = list(group)
        image = cv2.imread(str(photo_path(group[0].photo.file_name)))
        if image is None:
            logger.warning("event=embedding_photo_missing photo=%s", group[0].photo_id)
            continue
        done += embed_faces(image, group)
    if done:
        logger.info("event=embeddings_computed count=%d", done)
    return done
