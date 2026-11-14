import datetime
import json
import os
import shutil
import subprocess
import time

import pytest
from django.urls import reverse

from catalog.models import Face, Person, Photo
from exporter.models import ExportRequest
from exporter.services import backup, export, xmp

pytestmark = pytest.mark.django_db

EXIFTOOL = shutil.which(os.environ.get("EXIFTOOL_PATH", "exiftool"))
needs_exiftool = pytest.mark.skipif(EXIFTOOL is None, reason="exiftool is not installed")


@pytest.fixture
def cataloged(photo, boda):
    first, second = photo.faces.all()
    first.person = Person.objects.create(display_name="Luis, el primo")
    first.save()
    second.person = Person.objects.create(display_name="Ana")
    second.save()
    photo.event_type = boda
    photo.event_description = "Boda de Luis y Ana"
    photo.date_precision = Photo.DatePrecision.YEAR
    photo.date_value = datetime.date(1985, 1, 1)
    photo.status = Photo.Status.CATALOGED
    photo.save()
    return photo


def test_regions_are_normalized_and_centered(cataloged):
    regions = {r["name"]: r for r in xmp.face_regions(cataloged)}

    # Face at x=100, y=100, w=80, h=100 on an 800x600 photo.
    luis = regions["Luis, el primo"]
    assert luis["x"] == pytest.approx((100 + 40) / 800)
    assert luis["y"] == pytest.approx((100 + 50) / 600)
    assert luis["w"] == pytest.approx(80 / 800)
    assert luis["h"] == pytest.approx(100 / 600, abs=1e-6)


def test_removed_and_unknown_faces_are_not_exported(cataloged):
    Face.objects.filter(person__display_name="Ana").update(status=Face.Status.REMOVED)
    assert [r["name"] for r in xmp.face_regions(cataloged)] == ["Luis, el primo"]


def test_tags_and_dates(cataloged):
    assert xmp.tags(cataloged) == [
        "Eventos/Boda",
        "Eventos/Boda/Boda de Luis y Ana",
        "Fecha/aproximada",
    ]
    assert xmp.date_time_original(cataloged) == "1985:01:01 00:00:00"
    assert xmp.export_folder(cataloged) == "1985"

    cataloged.date_precision = Photo.DatePrecision.EXACT
    cataloged.date_value = datetime.date(1985, 7, 14)
    assert xmp.date_time_original(cataloged) == "1985:07:14 00:00:00"
    assert "Fecha/aproximada" not in xmp.tags(cataloged)

    cataloged.date_precision = Photo.DatePrecision.DECADE
    cataloged.date_value = datetime.date(1980, 1, 1)
    assert xmp.date_time_original(cataloged) is None
    assert xmp.export_folder(cataloged) == "sin-fecha"
    assert "Fecha/Década de 1980" in xmp.tags(cataloged)


def test_struct_values_are_escaped():
    assert xmp.escape_struct_value("Luis, el {primo}") == "Luis|, el |{primo|}"


def test_only_changed_cataloged_photos_are_exported(cataloged):
    assert list(export.photos_to_export()) == [cataloged]
    Photo.objects.filter(pk=cataloged.pk).update(
        exported_at=datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC)
    )
    assert list(export.photos_to_export()) == []


def test_export_request_from_admin_screen(client, admin_user):
    client.force_login(admin_user)
    client.post(reverse("export_start"))
    client.post(reverse("export_start"))  # second click does not queue twice
    assert ExportRequest.objects.filter(status=ExportRequest.Status.PENDING).count() == 1


@needs_exiftool
def test_exported_xmp_contains_mwg_regions(cataloged, settings):
    settings.EXIFTOOL_PATH = EXIFTOOL
    ExportRequest.objects.create()

    assert export.run_pending_requests() == 1

    jpg = settings.EXPORT_DIR / "1985" / cataloged.file_name
    sidecar = settings.EXPORT_DIR / "1985" / f"{cataloged.file_name}.xmp"
    assert jpg.exists() and sidecar.exists()
    assert ExportRequest.objects.get().status == ExportRequest.Status.DONE

    data = json.loads(
        subprocess.run(
            [EXIFTOOL, "-j", "-struct", "-XMP:all", str(sidecar)],
            capture_output=True,
            check=True,
            text=True,
        ).stdout
    )[0]
    region_info = data["RegionInfo"]
    assert region_info["AppliedToDimensions"] == {"W": 800, "H": 600, "Unit": "pixel"}
    regions = {r["Name"]: r for r in region_info["RegionList"]}
    assert set(regions) == {"Luis, el primo", "Ana"}
    luis = regions["Luis, el primo"]
    assert luis["Type"] == "Face"
    assert luis["Area"]["Unit"] == "normalized"
    assert luis["Area"]["X"] == pytest.approx(0.175)
    assert luis["Area"]["Y"] == pytest.approx(0.25)
    assert luis["Area"]["W"] == pytest.approx(0.1)
    assert luis["Area"]["H"] == pytest.approx(1 / 6, abs=1e-5)
    assert "Eventos/Boda" in data["Subject"]
    assert "Eventos/Boda/Boda de Luis y Ana" in data["TagsList"]
    assert "Fecha/aproximada" in data["TagsList"]
    assert data["DateTimeOriginal"].startswith("1985:01:01")

    cataloged.refresh_from_db()
    assert cataloged.exported_at is not None
    assert cataloged.export_path == f"1985/{cataloged.file_name}"


@needs_exiftool
def test_reexport_moves_files_when_year_changes(cataloged, settings):
    settings.EXIFTOOL_PATH = EXIFTOOL
    export.export_pending()
    cataloged.refresh_from_db()
    cataloged.date_value = datetime.date(1990, 1, 1)
    cataloged.catalog_changed_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(1)
    cataloged.save()

    export.export_pending()

    assert not (settings.EXPORT_DIR / "1985" / cataloged.file_name).exists()
    assert (settings.EXPORT_DIR / "1990" / f"{cataloged.file_name}.xmp").exists()


def test_backup_pruning(tmp_path):
    old = tmp_path / "phototag-20200101-030000.dump"
    new = tmp_path / "phototag-20990101-030000.dump"
    other = tmp_path / "keep.txt"
    for path in (old, new, other):
        path.write_text("x")
    stale = time.time() - 15 * 86400
    os.utime(old, (stale, stale))
    os.utime(other, (stale, stale))

    deleted = backup.prune_backups(tmp_path, retention_days=14)

    assert deleted == [old]
    assert new.exists() and other.exists()


def test_backup_skips_non_postgres():
    assert backup.backup_database() is None
