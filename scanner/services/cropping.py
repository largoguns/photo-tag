"""Split an A4 scan into individual photos (spec section 3.2, steps 2-6, and 3.3)."""

from dataclasses import dataclass, field

import cv2
import numpy as np

MAX_ASPECT_RATIO = 4.0
MIN_PHOTOS = 1
MAX_PHOTOS = 6
MAX_OTSU_THRESHOLD = 235
LID_MARGIN = 12  # grey levels below the lid that already count as photo
MIN_TINT = 15  # saturation (0-255) that a colourless lid never reaches
TINT_MARGIN = 10
EDGE_STRIP_RATIO = 0.01  # strips thinner than 1% of the page are not photos


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


def _foreground_mask(image):
    """Binary mask of everything that does not look like the scanner lid.

    The lid is bright and colourless. Old prints often have cream borders and light areas
    (white clothes, sky) brighter than an Otsu cut, but they are slightly coloured, so the
    background is "as bright as the lid and unsaturated", not just "bright".
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    otsu, mask = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    if otsu > MAX_OTSU_THRESHOLD:
        # Almost empty page: Otsu would split the lid's noise; use a conservative cut.
        _, mask = cv2.threshold(blurred, MAX_OTSU_THRESHOLD, 255, cv2.THRESH_BINARY_INV)

    lid = float(np.percentile(blurred, 99))
    cut = min(MAX_OTSU_THRESHOLD, max(otsu, lid - LID_MARGIN))
    mask |= ((blurred < cut) * 255).astype(np.uint8)
    if image.ndim == 3:
        saturation = cv2.GaussianBlur(cv2.cvtColor(image, cv2.COLOR_BGR2HSV)[:, :, 1], (5, 5), 0)
        lid_saturation = float(np.percentile(saturation[blurred >= lid - 2], 50))
        tinted = saturation > max(MIN_TINT, lid_saturation + TINT_MARGIN)
        mask |= (tinted * 255).astype(np.uint8)

    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    k = max(3, int(min(gray.shape) * 0.006) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    # Thin strips (the dark band outside the glass, lid edges) would glue photos that touch
    # the border together: an opening wider than those strips removes them.
    k = max(5, int(min(gray.shape) * EDGE_STRIP_RATIO) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
    # Outside the page counts as background, or strips along the edge would survive.
    return cv2.morphologyEx(
        mask, cv2.MORPH_OPEN, kernel, borderType=cv2.BORDER_CONSTANT, borderValue=0
    )


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


def reading_order(rects):
    """Top to bottom, then left to right: photos whose vertical spans mostly overlap share a row."""
    rows = []
    for rect in sorted(rects, key=lambda r: r[0][1]):
        top, bottom = rect[0][1] - rect[1][1] / 2, rect[0][1] + rect[1][1] / 2
        row = rows[-1] if rows else None
        if row and min(bottom, row["bottom"]) - max(top, row["top"]) > 0.5 * min(
            bottom - top, row["bottom"] - row["top"]
        ):
            row["rects"].append(rect)
            row["top"], row["bottom"] = min(top, row["top"]), max(bottom, row["bottom"])
        else:
            rows.append({"top": top, "bottom": bottom, "rects": [rect]})
    return [rect for row in rows for rect in sorted(row["rects"], key=lambda r: r[0][0])]


def split_scan(image, min_area_ratio=0.03, inset=4):
    """Detect the photos in a BGR scan image and return them deskewed and cropped."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    mask = _foreground_mask(image)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    min_area = min_area_ratio * gray.shape[0] * gray.shape[1]
    rects = []
    for contour in contours:
        if cv2.contourArea(contour) < min_area:
            continue
        rects.append(normalize_rect(cv2.minAreaRect(contour)))

    rects = reading_order(rects)

    result = CropResult()
    if not MIN_PHOTOS <= len(rects) <= MAX_PHOTOS:
        result.review_reasons.append({"code": "photo_count", "count": len(rects)})

    if any(_aspect_ratio(rect) > MAX_ASPECT_RATIO for rect in rects):
        result.review_reasons.append({"code": "aspect_ratio"})

    for i, a in enumerate(rects):
        if any(_overlaps(a, b) for b in rects[i + 1 :]):
            result.review_reasons.append({"code": "overlap"})
            break

    for rect in rects:
        warped = _warp(image, rect)
        hh, ww = warped.shape[:2]
        if inset and hh > 2 * inset + 1 and ww > 2 * inset + 1:
            warped = warped[inset : hh - inset, inset : ww - inset]
        result.crops.append(Crop(image=warped, center=rect[0], size=rect[1], angle=rect[2]))
    return result
