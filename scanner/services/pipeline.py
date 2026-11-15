"""End-to-end processing of the scans inbox (spec sections 3.2-3.4)."""

import hashlib
import logging
import math
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from catalog.models import Face, Photo, Scan, Upload
from catalog.services.voting import refresh_photo_status
from scanner.services import cropping, dedupe, embeddings, faces, images, orientation, uploads

logger = logging.getLogger("scanner")

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
# Files modified more recently than this may still be being copied to the inbox.
SETTLE_SECONDS = 5
# Overlap above which a newly detected face duplicates a kept one on reprocessing.
FACE_DUPLICATE_IOU = 0.3


class ScanError(Exception):
    pass


@dataclass
class ProcessedPhoto:
    crop: cropping.Crop
    faces: list[faces.DetectedFace]
    rotation: int = 0  # clockwise degrees applied to fix a sideways/upside-down photo


def sha256_of(data):
    return hashlib.sha256(data).hexdigest()


def decode_image(data):
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ScanError("Unreadable image")
    return image


def photo_file_name(sha, index):
    return f"{sha[:12]}_{index:02d}.jpg"


def inbox_files():
    inbox = Path(settings.SCANS_INBOX_DIR)
    if not inbox.is_dir():
        return []
    now = time.time()
    return sorted(
        p
        for p in inbox.iterdir()
        if p.is_file()
        and p.suffix.lower() in SUPPORTED_EXTENSIONS
        and now - p.stat().st_mtime >= SETTLE_SECONDS
    )


def _unique_destination(directory, name, sha):
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    if target.exists():
        target = directory / f"{Path(name).stem}_{sha[:8]}{Path(name).suffix}"
    counter = 1
    while target.exists():
        target = directory / f"{Path(name).stem}_{sha[:8]}_{counter}{Path(name).suffix}"
        counter += 1
    return target


def _move_to_processed(path, sha):
    target = _unique_destination(Path(settings.SCANS_PROCESSED_DIR), path.name, sha)
    shutil.move(str(path), target)
    return target


def whole_image(image):
    """A single uploaded photo: no cropping, the whole image is the photo."""
    height, width = image.shape[:2]
    crop = cropping.Crop(image=image, center=(width / 2, height / 2), size=(width, height), angle=0)
    return cropping.CropResult(crops=[crop])


def analyse(image, single=False):
    """Crop the photos of a scan (or take a single photo whole) and detect faces."""
    if single:
        result = whole_image(image)
    else:
        result = cropping.split_scan(
            image, min_area_ratio=settings.MIN_PHOTO_AREA_RATIO, inset=settings.CROP_INSET_PX
        )
    reasons = list(result.review_reasons)
    processed = []
    for crop in result.crops:
        found = orientation.detect_rotation(crop.image)
        if found.degrees:
            crop.image = orientation.rotate(crop.image, found.degrees)
        if found.uncertain and {"code": "orientation"} not in reasons:
            reasons.append({"code": "orientation"})
        processed.append(
            ProcessedPhoto(crop=crop, faces=_detect(crop.image), rotation=found.degrees)
        )
    return processed, reasons


def _detect(image):
    return faces.detect_faces(image, settings.YUNET_MODEL_PATH, settings.FACE_SCORE_THRESHOLD)


def _reorient(item, degrees):
    """Rotate a processed photo to an absolute orientation (e.g. a manual one to keep)."""
    delta = (degrees - item.rotation) % 360
    if delta:
        item.crop.image = orientation.rotate(item.crop.image, delta)
        item.faces = _detect(item.crop.image)
        item.rotation = degrees % 360


def _fingerprint(photo, image):
    """Store the perceptual hash; return the id of an earlier near-identical photo, if any."""
    photo.phash = dedupe.dhash(image)
    duplicate = dedupe.find_duplicate(dedupe.hash_variants(image), exclude_pk=photo.pk)
    if duplicate and not photo.duplicate_of_id:
        photo.duplicate_of_id = duplicate
    Photo.objects.filter(pk=photo.pk).update(phash=photo.phash, duplicate_of=photo.duplicate_of_id)
    return duplicate


def _flag_duplicates(scan, duplicates):
    reasons = [
        r for r in scan.review_reasons if not (isinstance(r, dict) and r.get("code") == "duplicate")
    ]
    reasons += [{"code": "duplicate", "photo": pk} for pk in duplicates]
    if duplicates:
        Scan.objects.filter(pk=scan.pk).update(
            needs_review=True, reviewed=False, review_reasons=reasons
        )
        scan.needs_review, scan.review_reasons = True, reasons


def _write_files(sha, processed):
    names = []
    for index, item in enumerate(processed):
        name = photo_file_name(sha, index)
        images.save_photo(item.crop.image, name)
        images.make_thumbnails(name)
        names.append(name)
    return names


def _create_faces(photo, detected):
    Face.objects.bulk_create(
        Face(photo=photo, x=f.x, y=f.y, w=f.w, h=f.h, score=f.score, source=Face.Source.AUTO)
        for f in detected
    )


def _embed_photos(pairs):
    """Compute missing face embeddings with the crops still in memory (after commit)."""
    for image, photo in pairs:
        missing = photo.faces.filter(
            embedding__isnull=True, status__in=[Face.Status.ACTIVE, Face.Status.PROPOSED]
        )
        embeddings.embed_faces(image, list(missing))


def process_file(path):
    """Process one scan from the inbox. Returns the new Scan, or None if already known."""
    path = Path(path)
    data = path.read_bytes()
    sha = sha256_of(data)
    upload = uploads.pending_upload_for(sha)
    try:
        return _process(path, data, sha, upload)
    except Exception:
        if upload is not None:
            Upload.objects.filter(pk=upload.pk).update(status=Upload.Status.FAILED)
        raise


def _process(path, data, sha, upload):
    if Scan.objects.filter(sha256=sha).exists():
        if upload is not None:
            Upload.objects.filter(pk=upload.pk).update(status=Upload.Status.DUPLICATE)
        target = _move_to_processed(path, sha)
        logger.info(
            "event=scan_skipped reason=duplicate file=%s sha=%s moved_to=%s",
            path.name,
            sha[:12],
            target.name,
        )
        return None

    started = time.monotonic()
    single = upload is not None and upload.kind == Upload.Kind.SINGLE
    processed, reasons = analyse(decode_image(data), single=single)
    names = _write_files(sha, processed)
    target = _unique_destination(Path(settings.SCANS_PROCESSED_DIR), path.name, sha)
    created = []
    try:
        with transaction.atomic():
            scan = Scan.objects.create(
                sha256=sha,
                file_name=target.name,
                needs_review=bool(reasons),
                review_reasons=reasons,
                uploaded_by=upload.user if upload else None,
            )
            if upload is not None:
                Upload.objects.filter(pk=upload.pk).update(
                    status=Upload.Status.PROCESSED, scan=scan
                )
            for index, (item, name) in enumerate(zip(processed, names, strict=True)):
                photo = Photo.objects.create(
                    scan=scan,
                    file_name=name,
                    index=index,
                    width=item.crop.image.shape[1],
                    height=item.crop.image.shape[0],
                    angle=round(item.crop.angle, 2),
                    crop_rect=item.crop.rect,
                    rotation=item.rotation,
                )
                _create_faces(photo, item.faces)
                created.append((item.crop.image, photo))
            duplicates = [d for image, photo in created if (d := _fingerprint(photo, image))]
            _flag_duplicates(scan, duplicates)
    except Exception:
        for name in names:
            images.delete_photo_files(name)
        raise
    shutil.move(str(path), target)
    _embed_photos(created)
    logger.info(
        "event=scan_processed file=%s sha=%s photos=%d faces=%d needs_review=%s seconds=%.1f",
        path.name,
        sha[:12],
        len(processed),
        sum(len(p.faces) for p in processed),
        scan.needs_review,
        time.monotonic() - started,
    )
    return scan


def process_inbox(skip=None):
    """Process every settled scan in the inbox. `skip` collects files that failed."""
    results = []
    for path in inbox_files():
        key = (path.name, path.stat().st_mtime, path.stat().st_size)
        if skip is not None and key in skip:
            continue
        try:
            results.append(process_file(path))
        except Exception as exc:
            logger.exception("event=scan_failed file=%s error=%s", path.name, exc)
            if skip is not None:
                skip.add(key)
    return [scan for scan in results if scan]


# ---------------------------------------------------------------------------
# Reprocessing (--force)
# ---------------------------------------------------------------------------


def find_scan(identifier):
    """Locate a scan by hash (or hash prefix) or by file name."""
    identifier = str(identifier).strip()
    name = Path(identifier).name
    scan = Scan.objects.filter(file_name=name).first()
    if scan:
        return scan
    if len(identifier) >= 6:
        matches = list(Scan.objects.filter(sha256__startswith=identifier.lower())[:2])
        if len(matches) == 1:
            return matches[0]
    for directory in (settings.SCANS_INBOX_DIR, settings.SCANS_PROCESSED_DIR):
        candidate = Path(directory) / name
        if candidate.is_file():
            return Scan.objects.filter(sha256=sha256_of(candidate.read_bytes())).first()
    return None


def _source_file(scan):
    for directory in (settings.SCANS_PROCESSED_DIR, settings.SCANS_INBOX_DIR):
        candidate = Path(directory) / scan.file_name
        if candidate.is_file():
            return candidate
    raise ScanError(f"Original file for scan {scan.sha256[:12]} not found")


def _match_photos(old_photos, processed):
    """Pair each new crop with the old photo at the same place on the scan (or None)."""
    remaining = list(old_photos)
    matches = []
    for item in processed:
        (cx, cy), (w, h) = item.crop.center, item.crop.size
        tolerance = 0.25 * min(w, h)
        best, best_distance = None, tolerance
        for old in remaining:
            rect = old.crop_rect or {}
            if "cx" not in rect:
                continue
            distance = math.hypot(rect["cx"] - cx, rect["cy"] - cy)
            if distance <= best_distance:
                best, best_distance = old, distance
        if best is not None:
            remaining.remove(best)
        matches.append(best)
    return matches, remaining


def _iou(a, b):
    ax1, ay1, bx1, by1 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw = max(0, min(ax1, bx1) - max(a[0], b[0]))
    ih = max(0, min(ay1, by1) - max(a[1], b[1]))
    inter = iw * ih
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union else 0.0


def _refresh_faces(photo, old_size, detected):
    """Keep faces with human input (rescaled), replace untouched automatic ones."""
    sx, sy = photo.width / old_size[0], photo.height / old_size[1]
    kept = []
    for face in photo.faces.all():
        touched = face.person_id or face.source == Face.Source.MANUAL or face.proposals.exists()
        if not touched:
            face.delete()
            continue
        face.x = min(round(face.x * sx), photo.width - 1)
        face.y = min(round(face.y * sy), photo.height - 1)
        face.w = max(1, min(round(face.w * sx), photo.width - face.x))
        face.h = max(1, min(round(face.h * sy), photo.height - face.y))
        face.embedding = None  # the crop changed: recompute
        face.save(update_fields=["x", "y", "w", "h", "embedding"])
        kept.append((face.x, face.y, face.w, face.h))
    fresh = [
        f for f in detected if all(_iou((f.x, f.y, f.w, f.h), k) < FACE_DUPLICATE_IOU for k in kept)
    ]
    _create_faces(photo, fresh)


def reprocess(scan):
    """Re-run cropping for a known scan, preserving photos (and their votes) that still exist."""
    source = _source_file(scan)
    single = scan.uploads.filter(kind=Upload.Kind.SINGLE).exists()
    processed, reasons = analyse(decode_image(source.read_bytes()), single=single)
    old_photos = list(scan.photos.all())
    matches, unmatched = _match_photos(old_photos, processed)
    # Kept photos keep their current orientation (it may have been fixed by hand).
    for item, old in zip(processed, matches, strict=True):
        if old is not None:
            _reorient(item, old.rotation)

    for old in old_photos:
        images.delete_photo_files(old.file_name)
    names = _write_files(scan.sha256, processed)
    refreshed = []

    with transaction.atomic():
        for old in unmatched:
            old.delete()
        # Free the (scan, index) slots before reassigning indices.
        for old in old_photos:
            if old not in unmatched:
                Photo.objects.filter(pk=old.pk).update(index=1000 + old.index)
        now = timezone.now()
        for index, (item, name, old) in enumerate(zip(processed, names, matches, strict=True)):
            height, width = item.crop.image.shape[:2]
            if old is None:
                photo = Photo.objects.create(
                    scan=scan,
                    file_name=name,
                    index=index,
                    width=width,
                    height=height,
                    angle=round(item.crop.angle, 2),
                    crop_rect=item.crop.rect,
                    rotation=item.rotation,
                )
                _create_faces(photo, item.faces)
                refreshed.append((item.crop.image, photo))
                continue
            old_size = (old.width, old.height)
            old.file_name, old.index, old.width, old.height = name, index, width, height
            old.angle = round(item.crop.angle, 2)
            old.crop_rect = item.crop.rect
            old.catalog_changed_at = now
            old.save()
            _refresh_faces(old, old_size, item.faces)
            refresh_photo_status(old)
            refreshed.append((item.crop.image, old))

        scan.needs_review = bool(reasons)
        scan.review_reasons = reasons
        scan.reviewed = False
        scan.processed_at = now
        scan.save()
        duplicates = [d for image, photo in refreshed if (d := _fingerprint(photo, image))]
        _flag_duplicates(scan, duplicates)

    _embed_photos(refreshed)
    logger.info(
        "event=scan_reprocessed sha=%s photos=%d kept=%d removed=%d needs_review=%s",
        scan.sha256[:12],
        len(processed),
        sum(1 for m in matches if m),
        len(unmatched),
        scan.needs_review,
    )
    return scan
