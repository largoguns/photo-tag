"""Detect photos placed sideways or upside down, using face detection (spec section 3.2).

YuNet barely finds faces that are not upright, so the orientation with the most (and most
confident) faces is the right one. Photos without faces are left as they are.
"""

from dataclasses import dataclass

import cv2
from django.conf import settings

from scanner.services import faces

ROTATIONS = (0, 90, 180, 270)
DETECTION_SIDE = 640
_CV2_CODES = {
    90: cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
}


@dataclass
class Orientation:
    degrees: int  # clockwise rotation to apply
    uncertain: bool  # another orientation looks likely but not clearly enough
    scores: dict


def rotate(image, degrees):
    """Rotate a BGR image clockwise by 0/90/180/270 degrees."""
    degrees %= 360
    return image if degrees == 0 else cv2.rotate(image, _CV2_CODES[degrees])


def _small(image):
    height, width = image.shape[:2]
    scale = min(1.0, DETECTION_SIDE / max(height, width))
    if scale == 1.0:
        return image
    return cv2.resize(
        image, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA
    )


def _score(image):
    found = faces.detect_faces(image, settings.YUNET_MODEL_PATH, settings.FACE_SCORE_THRESHOLD)
    return sum(face.score for face in found)


def detect_rotation(image):
    small = _small(image)
    scores = {degrees: _score(rotate(small, degrees)) for degrees in ROTATIONS}
    best = max(ROTATIONS, key=lambda d: (scores[d], d == 0))  # ties keep the photo as is
    second = max(score for degrees, score in scores.items() if degrees != best)
    if best == 0 or scores[best] < settings.ORIENTATION_MIN_SCORE:
        return Orientation(0, False, scores)
    if scores[best] >= settings.ORIENTATION_MARGIN * second:
        return Orientation(best, False, scores)
    return Orientation(0, True, scores)


def rotate_box(x, y, w, h, width, height, degrees):
    """Face box after rotating a width×height image clockwise by `degrees`."""
    degrees %= 360
    if degrees == 90:
        return height - (y + h), x, h, w
    if degrees == 180:
        return width - (x + w), height - (y + h), w, h
    if degrees == 270:
        return y, width - (x + w), h, w
    return x, y, w, h
