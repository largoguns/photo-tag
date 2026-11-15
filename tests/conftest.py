import cv2
import numpy as np
import pytest

from accounts.models import Role, User
from catalog.models import EventType, Face, Photo, Scan


@pytest.fixture
def data_dirs(settings, tmp_path):
    """Point every data directory at a fresh temporary tree."""
    settings.DATA_DIR = tmp_path
    settings.SCANS_INBOX_DIR = tmp_path / "scans" / "inbox"
    settings.SCANS_PROCESSED_DIR = tmp_path / "scans" / "processed"
    settings.PHOTOS_DIR = tmp_path / "photos"
    settings.THUMBS_DIR = tmp_path / "photos" / "thumbs"
    settings.EXPORT_DIR = tmp_path / "export"
    settings.BACKUPS_DIR = tmp_path / "backups"
    for path in (
        settings.SCANS_INBOX_DIR,
        settings.SCANS_PROCESSED_DIR,
        settings.THUMBS_DIR,
        settings.EXPORT_DIR,
        settings.BACKUPS_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)
    return tmp_path


def make_user(username, role=Role.COLLABORATOR, **extra):
    user = User.objects.create_user(username=username, password="Contrasena-segura-1", **extra)
    user.role = role
    user.must_change_password = extra.get("must_change_password", False)
    user.save()
    return user


@pytest.fixture
def superadmin(db):
    return make_user("root", Role.SUPERADMIN)


@pytest.fixture
def admin_user(db):
    return make_user("admin1", Role.ADMIN)


@pytest.fixture
def alice(db):
    return make_user("alice", display_name="Alicia")


@pytest.fixture
def bob(db):
    return make_user("bob", display_name="Roberto")


@pytest.fixture
def carol(db):
    return make_user("carol", display_name="Carolina")


@pytest.fixture
def boda(db):
    return EventType.objects.get(name="Boda")


@pytest.fixture
def photo(db, data_dirs):
    """A stored 800x600 photo with two automatically detected faces."""
    scan = Scan.objects.create(sha256="a" * 64, file_name="scan.jpg")
    image = np.full((600, 800, 3), 128, np.uint8)
    file_name = "aaaaaaaaaaaa_00.jpg"
    from scanner.services import images

    images.save_photo(image, file_name)
    images.make_thumbnails(file_name)
    photo = Photo.objects.create(scan=scan, file_name=file_name, index=0, width=800, height=600)
    Face.objects.create(photo=photo, x=100, y=100, w=80, h=100, score=0.9)
    Face.objects.create(photo=photo, x=500, y=200, w=60, h=80, score=0.8)
    return photo


def synthetic_scan(specs, size=(1754, 1240), seed=0):
    """White A4-like page with coloured rotated rectangles: specs = [(center, size, angle)]."""
    rng = np.random.default_rng(seed)
    page = np.full((*size, 3), 248, np.uint8)
    page = cv2.add(page, rng.integers(0, 6, page.shape, dtype=np.uint8))
    for center, rect_size, angle in specs:
        box = cv2.boxPoints((center, rect_size, angle)).astype(np.int32)
        color = [int(c) for c in rng.integers(30, 190, 3)]
        cv2.fillPoly(page, [box], color)
        # A different composition per photo (blobs at random spots), like real pictures.
        radius = int(min(rect_size) / 6)
        for _ in range(3):
            dx, dy = (rng.uniform(-0.3, 0.3, 2) * np.array(rect_size)).astype(int)
            cx, cy = int(center[0]) + int(dx), int(center[1]) + int(dy)
            shade = [int(c) for c in rng.integers(0, 150, 3)]
            cv2.circle(page, (cx, cy), radius, shade, -1)
    return page


FOUR_PHOTOS = [
    ((330, 420), (500, 360), -8),
    ((900, 430), (380, 520), 5),
    ((330, 1250), (520, 380), 12),
    ((900, 1250), (400, 500), -3),
]
