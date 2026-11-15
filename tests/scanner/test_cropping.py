import cv2
import numpy as np
import pytest

from scanner.services.cropping import normalize_rect, reading_order, split_scan
from tests.conftest import FOUR_PHOTOS, synthetic_scan


def test_detects_four_rotated_photos_with_angles():
    result = split_scan(synthetic_scan(FOUR_PHOTOS))

    assert len(result.crops) == 4
    assert not result.needs_review
    expected = sorted(angle for _, _, angle in FOUR_PHOTOS)
    detected = sorted(crop.angle for crop in result.crops)
    for want, got in zip(expected, detected, strict=True):
        assert got == pytest.approx(want, abs=2)


@pytest.mark.parametrize("angle", [-30, -15, -2, 0, 7, 20, 35])
def test_angle_tolerance_single_photo(angle):
    result = split_scan(synthetic_scan([((620, 870), (700, 500), angle)]))

    assert len(result.crops) == 1
    assert result.crops[0].angle == pytest.approx(angle, abs=2)


def test_crops_are_deskewed_to_rectangle_size_minus_inset():
    result = split_scan(synthetic_scan([((620, 870), (700, 500), 10)]), inset=4)

    height, width = result.crops[0].image.shape[:2]
    assert width == pytest.approx(700 - 8, abs=6)
    assert height == pytest.approx(500 - 8, abs=6)


def test_crops_are_in_reading_order():
    result = split_scan(synthetic_scan(FOUR_PHOTOS))

    centers = [crop.center for crop in result.crops]
    assert centers[0][1] < centers[2][1]  # first row above second row
    assert centers[0][0] < centers[1][0]  # left to right within a row


def test_small_specks_are_ignored():
    specs = [*FOUR_PHOTOS, ((620, 1650), (40, 30), 0)]
    result = split_scan(synthetic_scan(specs))

    assert len(result.crops) == 4


def test_empty_scan_needs_review():
    result = split_scan(synthetic_scan([]))

    assert result.crops == []
    assert result.needs_review


def test_too_many_photos_needs_review():
    specs = [((200 + 420 * (i % 3), 300 + 560 * (i // 3)), (300, 260), 0) for i in range(7)]
    specs[6] = ((620, 1550), (300, 260), 0)
    result = split_scan(synthetic_scan(specs, size=(1754, 1240)))

    assert len(result.crops) == 7
    assert result.needs_review


def test_anomalous_aspect_ratio_needs_review():
    result = split_scan(synthetic_scan([((620, 500), (900, 180), 0), ((620, 1200), (500, 400), 0)]))

    assert result.needs_review
    assert {"code": "aspect_ratio"} in result.review_reasons


def test_normalize_rect_folds_angle():
    (_, (w, h), angle) = normalize_rect(((10, 10), (100, 50), 80))
    assert angle == pytest.approx(-10)
    assert (w, h) == (50, 100)


# --- Regressions from a real scan of old prints (2026-11-15) ---------------------------


def test_cream_border_and_white_areas_stay_in_one_photo():
    # Old prints: cream border brighter than an Otsu cut, and a white blanket crossing
    # the whole picture. The tint of the border keeps the print in one piece.
    page = synthetic_scan([])
    cv2.rectangle(page, (300, 300), (900, 1200), (205, 238, 250), -1)  # cream border
    cv2.rectangle(page, (330, 330), (870, 1170), (60, 70, 120), -1)  # picture
    cv2.rectangle(page, (330, 700), (870, 800), (250, 250, 250), -1)  # white blanket
    result = split_scan(page)

    assert not result.needs_review
    (crop,) = result.crops
    assert sorted(crop.size) == pytest.approx([600, 900], abs=8)  # border included


def test_dark_band_at_the_page_edge_does_not_glue_photos():
    # The strip outside the glass is dark and joined two photos touching the bottom edge.
    specs = [((330, 1450), (500, 600), 0), ((900, 1450), (480, 600), 0)]
    page = synthetic_scan(specs)
    page[-8:, :] = 70
    result = split_scan(page)

    assert not result.needs_review
    assert len(result.crops) == 2


def test_reading_order_with_staggered_rows():
    left_lower = ((300, 600), (400, 700), 0)
    right_higher = ((900, 450), (400, 700), 0)
    below = ((600, 1400), (400, 300), 0)
    assert reading_order([below, right_higher, left_lower]) == [left_lower, right_higher, below]


def _textured(rng, size, base):
    """A print-like patch: a base colour with soft blobs (no long straight edges)."""
    w, h = size
    patch = np.full((h, w, 3), base, np.uint8)
    for _ in range(12):
        center = (int(rng.integers(0, w)), int(rng.integers(0, h)))
        color = [int(v) for v in rng.integers(0, 255, 3)]
        cv2.circle(patch, center, int(rng.integers(20, 80)), color, -1)
    return cv2.GaussianBlur(patch, (0, 0), 3)


def test_photos_touching_each_other_are_split_at_the_seam():
    # Regression (2026-11-15): two prints stacked with no lid between them.
    rng = np.random.default_rng(3)
    page = synthetic_scan([])
    page[200:800, 300:1000] = _textured(rng, (700, 600), (60, 90, 140))
    page[800:1400, 300:1000] = _textured(rng, (700, 600), (150, 120, 70))
    page[797:803, 300:1000] = 50  # the thin shadow between two prints
    result = split_scan(page)

    assert {"code": "split"} in result.review_reasons
    assert len(result.crops) == 2
    for crop in result.crops:
        assert sorted(crop.size) == pytest.approx([600, 700], abs=20)


def test_a_long_photo_without_seam_is_not_split():
    rng = np.random.default_rng(4)
    page = synthetic_scan([])
    page[150:1450, 400:950] = _textured(rng, (550, 1300), (90, 110, 60))  # aspect 2.4
    result = split_scan(page)

    assert len(result.crops) == 1
    assert {"code": "split"} not in result.review_reasons


def test_touching_prints_with_borders_are_split_between_the_borders():
    # Prints with cream borders: split in the middle, not at the inner edge of a border.
    rng = np.random.default_rng(5)
    page = synthetic_scan([])
    for top in (200, 800):
        page[top : top + 600, 300:1000] = (205, 238, 250)  # cream border
        page[top + 30 : top + 570, 330:970] = _textured(rng, (640, 540), (80, 100, 130))
    result = split_scan(page)

    assert len(result.crops) == 2
    centers = sorted(crop.center[1] for crop in result.crops)
    assert centers == pytest.approx([500, 1100], abs=8)
    for crop in result.crops:
        assert sorted(crop.size) == pytest.approx([600, 700], abs=12)
