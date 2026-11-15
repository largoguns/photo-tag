"""Corner detection of a paper print photographed with a phone."""

import cv2
import numpy as np
import pytest

from scanner.services.perspective import detect_quad

CORNERS = [[310, 180], [1420, 260], [1350, 1080], [240, 980]]  # a print seen at an angle


def phone_photo(corners=CORNERS, size=(1600, 1200), seed=0):
    """A textured print warped onto a wooden-ish table."""
    rng = np.random.default_rng(seed)
    width, height = size
    table = np.zeros((height, width, 3), np.uint8)
    table[:] = (60, 90, 130)
    noise = rng.normal(0, 12, (height, width, 1)).astype(np.int16)
    table = np.clip(table.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    print_ = np.full((800, 1200, 3), (215, 235, 245), np.uint8)  # cream border
    inner = print_[40:760, 40:1160]
    inner[:] = (90, 120, 80)
    for _ in range(15):
        center = (int(rng.integers(0, 1120)), int(rng.integers(0, 720)))
        color = [int(v) for v in rng.integers(0, 255, 3)]
        cv2.circle(inner, center, int(rng.integers(30, 120)), color, -1)
    src = np.float32([[0, 0], [1200, 0], [1200, 800], [0, 800]])
    matrix = cv2.getPerspectiveTransform(src, np.float32(corners))
    warped = cv2.warpPerspective(print_, matrix, size)
    mask = cv2.warpPerspective(np.full((800, 1200), 255, np.uint8), matrix, size)
    table[mask > 0] = warped[mask > 0]
    return table


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_finds_the_corners_of_a_print_seen_at_an_angle(seed):
    found = detect_quad(phone_photo(seed=seed))
    assert found is not None
    for (x, y), (ex, ey) in zip(found, CORNERS, strict=True):
        assert abs(x - ex) < 0.02 * 1600 and abs(y - ey) < 0.02 * 1200


def test_works_on_a_reduced_preview():
    # The editor detects on the scan preview, scaled back to scan pixels by the caller.
    small = cv2.resize(phone_photo(), (800, 600), interpolation=cv2.INTER_AREA)
    found = detect_quad(small)
    assert found is not None
    for (x, y), (ex, ey) in zip(found, CORNERS, strict=True):
        assert abs(x * 2 - ex) < 32 and abs(y * 2 - ey) < 24


def test_nothing_found_in_a_picture_without_a_print():
    rng = np.random.default_rng(9)
    picture = cv2.GaussianBlur(rng.integers(0, 255, (900, 1200, 3), dtype=np.uint8), (0, 0), 25)
    assert detect_quad(picture) is None
