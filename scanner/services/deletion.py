"""Deleting photos and scans, and reprocessing scans, from the web.

The web only changes the database; files are removed by the worker from FileCleanup
tasks, because it is the only container that can write photos, exports and scans.
Originals of deleted scans are moved to scans/deleted/, never removed.
"""

import logging
import shutil
from pathlib import Path

from django.conf import settings
from django.db import transaction

from accounts.services import audit
from catalog.models import FileCleanup, Photo, Proposal, Scan
from catalog.services import voting
from scanner.services import images, pipeline

logger = logging.getLogger("scanner")


def _queue_photo_files(photo):
    tasks = [FileCleanup(kind=FileCleanup.Kind.PHOTO, name=photo.file_name)]
    if photo.export_path:
        tasks.append(FileCleanup(kind=FileCleanup.Kind.EXPORT, name=photo.export_path))
    FileCleanup.objects.bulk_create(tasks)


def _remove_photo(photo):
    """Delete a photo row (faces, proposals and votes cascade) and queue its files."""
    proposals = Proposal.objects.filter(photo=photo).select_related("person", "event_type")
    provisional = [(p.person, p.event_type) for p in proposals]
    from gamification.services import scoring

    scoring.photo_removed(photo, deleted=True)
    _queue_photo_files(photo)
    photo.delete()
    for person, event_type in provisional:
        voting.cleanup_provisional_person(person)
        voting._cleanup_provisional_event_type(event_type)


def delete_photo(user, photo):
    with transaction.atomic():
        details = {
            "file": photo.file_name,
            "scan": photo.scan.file_name,
            "faces": photo.faces.count(),
        }
        audit.log(user, "photo.delete", photo, **details)
        _remove_photo(photo)


def delete_scan(user, scan):
    """Delete a scan and all its photos; its original is set aside in scans/deleted/."""
    with transaction.atomic():
        audit.log(user, "scan.delete", scan, file=scan.file_name, photos=scan.photos.count())
        for photo in list(scan.photos.all()):
            _remove_photo(photo)
        FileCleanup.objects.bulk_create(
            [
                FileCleanup(kind=FileCleanup.Kind.PREVIEW, name=scan.sha256),
                FileCleanup(kind=FileCleanup.Kind.ORIGINAL, name=scan.file_name),
            ]
        )
        scan.delete()  # its uploads and scan jobs keep their rows (scan becomes NULL)


def request_reprocess(user, scan):
    Scan.objects.filter(pk=scan.pk).update(reprocess_requested=True)
    scan.reprocess_requested = True
    audit.log(user, "scan.reprocess", scan)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def _run(task):
    if task.kind == FileCleanup.Kind.PHOTO:
        images.delete_photo_files(task.name)
    elif task.kind == FileCleanup.Kind.EXPORT:
        exported = Path(settings.EXPORT_DIR) / task.name
        exported.unlink(missing_ok=True)
        Path(f"{exported}.xmp").unlink(missing_ok=True)
    elif task.kind == FileCleanup.Kind.PREVIEW:
        images.preview_path(task.name).unlink(missing_ok=True)
    elif task.kind == FileCleanup.Kind.ORIGINAL:
        source = Path(settings.SCANS_PROCESSED_DIR) / task.name
        if source.exists():
            target_dir = Path(settings.SCANS_DELETED_DIR)
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / task.name
            if target.exists():  # the same name deleted twice: keep both
                target = target_dir / f"{target.stem}-{task.pk}{target.suffix}"
            shutil.move(str(source), target)


def run_cleanups(limit=50):
    """Worker job. Returns how many tasks were done; failures stay for the next run."""
    done = 0
    for task in FileCleanup.objects.all()[:limit]:
        try:
            _run(task)
        except Exception:
            logger.exception("event=file_cleanup_failed kind=%s name=%s", task.kind, task.name)
            continue
        task.delete()
        done += 1
    return done


def run_reprocess_requests(limit=5):
    """Worker job: reprocess the scans asked for from the web."""
    done = 0
    for scan in Scan.objects.filter(reprocess_requested=True)[:limit]:
        try:
            pipeline.reprocess(scan)
            done += 1
            logger.info("event=scan_reprocessed_from_web scan=%s", scan.sha256[:12])
        except Exception:
            logger.exception("event=scan_reprocess_failed scan=%s", scan.sha256[:12])
        finally:
            Scan.objects.filter(pk=scan.pk).update(reprocess_requested=False)
    return done


def next_after(photo):
    """Where to go once `photo` is deleted: another photo of its scan, if any."""
    return (
        Photo.objects.filter(scan_id=photo.scan_id).exclude(pk=photo.pk).order_by("index").first()
    )
