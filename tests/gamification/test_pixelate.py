"""Quiz images: sharp and pixelated (spec section 5.14)."""

from io import BytesIO

import pytest
from PIL import Image, ImageStat

from catalog.models import Photo, Scan
from gamification.services import pixelate

pytestmark = pytest.mark.django_db


@pytest.fixture
def striped(data_dirs):
    """A 1000x500 photo of thin vertical stripes (only a sharp image keeps them)."""
    scan = Scan.objects.create(sha256="p" * 64, file_name="pix.jpg")
    row = bytes(0 if x % 8 < 4 else 255 for x in range(1000))
    image = Image.frombytes("L", (1000, 500), row * 500).convert("RGB")
    (data_dirs / "photos").mkdir(exist_ok=True)
    image.save(data_dirs / "photos" / "pix_00.jpg", quality=95)
    return Photo.objects.create(scan=scan, file_name="pix_00.jpg", index=0, width=1000, height=500)


def contrast(data):
    """(size, standard deviation of the gray levels): stripes are contrast, blocks are not."""
    with Image.open(BytesIO(data)) as image:
        return image.size, ImageStat.Stat(image.convert("L")).stddev[0]


def test_sharp_image_is_resized_to_800(striped):
    size, spread = contrast(pixelate.render(striped))
    assert size == (800, 400) and spread > 60


def test_pixelated_levels_hide_detail(striped):
    for level in range(pixelate.SHARP):
        size, spread = contrast(pixelate.render(striped, level))
        assert size == (800, 400) and spread < 20


def test_blocks_are_flat(striped):
    with Image.open(BytesIO(pixelate.render(striped, 0))) as image:
        gray = image.convert("L")
        # Inside one block (800 / 6 ≈ 133 px) every pixel has about the same shade.
        values = [gray.getpixel((x, 50)) for x in range(10, 120)]
        assert max(values) - min(values) < 12


def test_missing_file_gives_none(db):
    scan = Scan.objects.create(sha256="m" * 64, file_name="none.jpg")
    photo = Photo.objects.create(scan=scan, file_name="none_00.jpg", index=0, width=10, height=10)
    assert pixelate.render(photo) is None
