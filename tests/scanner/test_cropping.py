import pytest

from scanner.services.cropping import normalize_rect, split_scan
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
    assert any("aspecto" in reason for reason in result.review_reasons)


def test_normalize_rect_folds_angle():
    (_, (w, h), angle) = normalize_rect(((10, 10), (100, 50), 80))
    assert angle == pytest.approx(-10)
    assert (w, h) == (50, 100)
