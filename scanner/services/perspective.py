"""Find a paper print in a phone photo: its 4 corners, as a suggestion for the editor.

Never applied on its own: an administrator confirms (or moves) the corners first.
"""

import cv2
import numpy as np

from scanner.services.cropping import _order_points

WORK_SIDE = 1000  # detect on a reduced copy
MIN_AREA = 0.2  # the print fills at least 20% of the picture
EPSILON = 0.02  # polygon simplification, share of the perimeter


def _quads(mask, min_area):
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in sorted(contours, key=cv2.contourArea, reverse=True):
        if cv2.contourArea(contour) < min_area:
            break
        hull = cv2.convexHull(contour)
        approx = cv2.approxPolyDP(hull, EPSILON * cv2.arcLength(hull, True), True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            yield approx.reshape(4, 2).astype(np.float32)


def _masks(gray):
    """Candidate foreground masks: edges first, then both Otsu polarities."""
    median = float(np.median(gray))
    edges = cv2.Canny(gray, int(max(0, 0.66 * median)), int(min(255, 1.33 * median)))
    edges = cv2.dilate(edges, np.ones((5, 5), np.uint8), iterations=2)
    # Fill what the edges enclose so the print becomes one solid blob.
    filled = edges.copy()
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(filled, contours, -1, 255, cv2.FILLED)
    yield filled
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    kernel = np.ones((7, 7), np.uint8)
    for mask in (otsu, cv2.bitwise_not(otsu)):
        yield cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)


def _on_frame(quad, shape, margin=0.02):
    """Three or more corners on the edges of the picture."""
    height, width = shape[:2]
    mx, my = margin * width, margin * height
    near = sum(x < mx or y < my or x > width - mx or y > height - my for x, y in quad)
    return near >= 3


def detect_quad(image):
    """[[x, y] × 4] (TL, TR, BR, BL) of the print in `image`, or None."""
    height, width = image.shape[:2]
    scale = min(1.0, WORK_SIDE / max(height, width))
    small = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if small.ndim == 3 else small
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    min_area = MIN_AREA * gray.shape[0] * gray.shape[1]
    total = gray.shape[0] * gray.shape[1]
    for mask in _masks(gray):
        for quad in _quads(mask, min_area):
            if cv2.contourArea(quad) > 0.98 * total or _on_frame(quad, gray.shape):
                continue  # the picture's own frame, not a print inside it
            corners = _order_points(quad) / scale
            return [[round(float(x), 1), round(float(y), 1)] for x, y in corners]
    return None
