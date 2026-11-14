"""Split an A4 scan into individual photos (spec section 3.2, steps 2-6, and 3.3)."""

from dataclasses import dataclass, field

import cv2
import numpy as np

MAX_ASPECT_RATIO = 4.0
MIN_PHOTOS = 1
MAX_PHOTOS = 6
MAX_OTSU_THRESHOLD = 235


@dataclass
class Crop:
    image: np.ndarray
    center: tuple[float, float]
    size: tuple[float, float]  # (width, height) of the rotated rectangle, before inset
    angle: float  # degrees in (-45, 45], OpenCV rotated-rect convention

    @property
    def rect(self):
        return {
            "cx": round(self.center[0], 1),
            "cy": round(self.center[1], 1),
            "w": round(self.size[0], 1),
            "h": round(self.size[1], 1),
            "angle": round(self.angle, 2),
        }


@dataclass
class CropResult:
    crops: list[Crop] = field(default_factory=list)
    review_reasons: list[str] = field(default_factory=list)

    @property
    def needs_review(self):
        return bool(self.review_reasons)


def normalize_rect(rect):
    """Return ((cx, cy), (w, h), angle) with the angle folded into (-45, 45]."""
    (cx, cy), (w, h), angle = rect
    while angle > 45:
        angle -= 90
        w, h = h, w
    while angle <= -45:
        angle += 90
        w, h = h, w
    return (cx, cy), (w, h), angle


def _foreground_mask(gray):
    """Binary mask of everything darker than the (white) scanner lid."""
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    threshold, mask = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    if threshold > MAX_OTSU_THRESHOLD:
        # Almost empty page: Otsu would split the lid's noise; use a conservative cut.
        _, mask = cv2.threshold(blurred, MAX_OTSU_THRESHOLD, 255, cv2.THRESH_BINARY_INV)

    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    k = max(3, int(min(gray.shape) * 0.006) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
    return cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)


def _order_points(points):
    """Order 4 points as top-left, top-right, bottom-right, bottom-left."""
    points = np.asarray(points, dtype=np.float32)
    s = points.sum(axis=1)
    d = np.diff(points, axis=1).ravel()
    return np.array(
        [points[np.argmin(s)], points[np.argmin(d)], points[np.argmax(s)], points[np.argmax(d)]],
        dtype=np.float32,
    )


def _warp(image, rect):
    (cx, cy), (w, h), angle = rect
    box = cv2.boxPoints(((cx, cy), (w, h), angle))
    src = _order_points(box)
    width, height = int(round(w)), int(round(h))
    dst = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], np.float32)
    matrix = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(
        image, matrix, (width, height), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE
    )


def _aspect_ratio(rect):
    w, h = rect[1]
    return max(w, h) / min(w, h) if min(w, h) else float("inf")


def _overlaps(rect_a, rect_b):
    kind, _ = cv2.rotatedRectangleIntersection(rect_a, rect_b)
    return kind != cv2.INTERSECT_NONE


def split_scan(image, min_area_ratio=0.03, inset=4):
    """Detect the photos in a BGR scan image and return them deskewed and cropped."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    mask = _foreground_mask(gray)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    min_area = min_area_ratio * gray.shape[0] * gray.shape[1]
    rects = []
    for contour in contours:
        if cv2.contourArea(contour) < min_area:
            continue
        rects.append(normalize_rect(cv2.minAreaRect(contour)))

    # Reading order: top to bottom, then left to right (rows of roughly equal height).
    if rects:
        row_height = max(min(r[1]) for r in rects) / 2
        rects.sort(key=lambda r: (round(r[0][1] / row_height), r[0][0]))

    result = CropResult()
    if not MIN_PHOTOS <= len(rects) <= MAX_PHOTOS:
        result.review_reasons.append(f"Se han detectado {len(rects)} fotos")

    if any(_aspect_ratio(rect) > MAX_ASPECT_RATIO for rect in rects):
        result.review_reasons.append("Relación de aspecto anómala")

    for i, a in enumerate(rects):
        if any(_overlaps(a, b) for b in rects[i + 1 :]):
            result.review_reasons.append("Recortes solapados")
            break

    for rect in rects:
        warped = _warp(image, rect)
        hh, ww = warped.shape[:2]
        if inset and hh > 2 * inset + 1 and ww > 2 * inset + 1:
            warped = warped[inset : hh - inset, inset : ww - inset]
        result.crops.append(Crop(image=warped, center=rect[0], size=rect[1], angle=rect[2]))
    return result
