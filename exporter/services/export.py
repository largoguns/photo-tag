"""Export cataloged photos with XMP sidecars to the folder Immich reads (spec section 6)."""

import logging
import shutil
from pathlib import Path

import exiftool
from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from catalog.models import Photo
from exporter.models import ExportRequest
from exporter.services import xmp
from scanner.services.images import photo_path

logger = logging.getLogger("exporter")


class ExportError(Exception):
    pass


def photos_to_export():
    """Cataloged photos whose cataloging changed since their last export."""
    return (
        Photo.objects.visible()
        .filter(status=Photo.Status.CATALOGED)
        .filter(
            Q(exported_at__isnull=True)
            | Q(catalog_changed_at__isnull=False, catalog_changed_at__gt=F("exported_at"))
        )
        .select_related("event_type")
    )


def _remove_previous(photo, new_relative):
    if photo.export_path and photo.export_path != new_relative:
        old = Path(settings.EXPORT_DIR) / photo.export_path
        old.unlink(missing_ok=True)
        Path(f"{old}.xmp").unlink(missing_ok=True)


def write_sidecar(photo, sidecar_path, tool):
    tmp = sidecar_path.with_name(f".{sidecar_path.name}.tmp.xmp")
    tmp.unlink(missing_ok=True)
    tool.execute(*xmp.exiftool_args(photo, tmp))
    if tool.last_status != 0 or not tmp.exists():
        raise ExportError(f"exiftool failed for photo {photo.pk}: {tool.last_stderr.strip()}")
    tmp.replace(sidecar_path)


def export_photo(photo, tool):
    folder = Path(settings.EXPORT_DIR) / xmp.export_folder(photo)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / photo.file_name
    relative = str(target.relative_to(settings.EXPORT_DIR))

    tmp = target.with_name(f".{target.name}.tmp")
    shutil.copyfile(photo_path(photo.file_name), tmp)
    tmp.replace(target)
    write_sidecar(photo, Path(f"{target}.xmp"), tool)
    _remove_previous(photo, relative)

    Photo.objects.filter(pk=photo.pk).update(exported_at=timezone.now(), export_path=relative)
    return target


def export_pending():
    """Export every photo that needs it. Returns (exported, failed) counts."""
    exported = failed = 0
    photos = list(photos_to_export())
    if not photos:
        return 0, 0
    with exiftool.ExifTool(executable=settings.EXIFTOOL_PATH) as tool:
        for photo in photos:
            try:
                export_photo(photo, tool)
                exported += 1
            except Exception:
                failed += 1
                logger.exception("event=export_photo_failed photo=%s", photo.pk)
    logger.info("event=export_done exported=%d failed=%d", exported, failed)
    return exported, failed


def request_export(user):
    from accounts.services import audit

    request = ExportRequest.objects.create(requested_by=user)
    audit.log(user, "export.request", request)
    return request


def run_pending_requests():
    """Worker job: run the export once for all pending requests."""
    with transaction.atomic():
        pending = list(
            ExportRequest.objects.select_for_update(skip_locked=True).filter(
                status=ExportRequest.Status.PENDING
            )
        )
        if not pending:
            return None
        ids = [r.pk for r in pending]
        ExportRequest.objects.filter(pk__in=ids).update(
            status=ExportRequest.Status.RUNNING, started_at=timezone.now()
        )
    try:
        exported, failed = export_pending()
    except Exception as exc:
        logger.exception("event=export_failed")
        ExportRequest.objects.filter(pk__in=ids).update(
            status=ExportRequest.Status.FAILED, finished_at=timezone.now(), error=str(exc)[:2000]
        )
        return None
    ExportRequest.objects.filter(pk__in=ids).update(
        status=ExportRequest.Status.FAILED if failed else ExportRequest.Status.DONE,
        finished_at=timezone.now(),
        exported_count=exported,
        error=f"{failed} fotos no se pudieron exportar; revisa los logs del worker."
        if failed
        else "",
    )
    return exported
