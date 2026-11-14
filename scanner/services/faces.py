"""Face detection with OpenCV YuNet (spec section 3.2, step 7)."""

from dataclasses import dataclass
from functools import lru_cache

import cv2

# YuNet is fast and accurate around this size; larger photos are downscaled for detection.
DETECTION_MAX_SIDE = 1280


@dataclass
class DetectedFace:
    x: int
    y: int
    w: int
    h: int
    score: float


@lru_cache(maxsize=4)
def _detector(model_path, score_threshold):
    return cv2.FaceDetectorYN.create(str(model_path), "", (320, 320), score_threshold, 0.3, 5000)


def detect_faces(image, model_path, score_threshold=0.7):
    """Return the faces found in a BGR image, in that image's pixel coordinates."""
    height, width = image.shape[:2]
    scale = min(1.0, DETECTION_MAX_SIDE / max(height, width))
    if scale < 1.0:
        image = cv2.resize(image, (round(width * scale), round(height * scale)), cv2.INTER_AREA)

    detector = _detector(str(model_path), float(score_threshold))
    detector.setInputSize((image.shape[1], image.shape[0]))
    _, detections = detector.detect(image)
    if detections is None:
        return []

    faces = []
    for row in detections:
        x, y, w, h = (float(v) / scale for v in row[:4])
        x0, y0 = max(0, round(x)), max(0, round(y))
        x1, y1 = min(width, round(x + w)), min(height, round(y + h))
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        faces.append(DetectedFace(x0, y0, x1 - x0, y1 - y0, round(float(row[-1]), 4)))
    return faces
