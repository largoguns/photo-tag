"""Manual photo rotation: requested from the web, applied by the worker.

The web container mounts the photos read-only, so a click only records the pending
rotation; the worker rewrites the file and thumbnails and moves the face boxes.
"""

import logging

import cv2
from django.db import transaction
from django.utils import timezone

from accounts.services import audit
from catalog.models import Face, Photo, Proposal
from scanner.services import dedupe, images
from scanner.services.orientation import rotate, rotate_box

logger = logging.getLogger("scanner")


def request_rotation(user, photo, degrees=90):
    """Queue a clockwise rotation (accumulates with pending clicks)."""
    with transaction.atomic():
        photo = Photo.objects.select_for_update().get(pk=photo.pk)
        photo.pending_rotation = (photo.pending_rotation + degrees) % 360
        photo.save(update_fields=["pending_rotation"])
    audit.log(user, "photo.rotate", photo, degrees=degrees)
    return photo


def _apply(photo):
    degrees = photo.pending_rotation % 360
    image = cv2.imread(str(images.photo_path(photo.file_name)))
    if image is None:
        raise OSError(f"Photo file missing: {photo.file_name}")
    rotated = rotate(image, degrees)
    images.save_photo(rotated, photo.file_name)
    images.make_thumbnails(photo.file_name)

    width, height = photo.width, photo.height
    for face in Face.objects.filter(photo=photo):
        face.x, face.y, face.w, face.h = rotate_box(
            face.x, face.y, face.w, face.h, width, height, degrees
        )
        face.embedding = None  # recomputed by the worker on the new orientation
        face.save(update_fields=["x", "y", "w", "h", "embedding"])
    for proposal in Proposal.objects.filter(photo=photo, kind=Proposal.Kind.ADD_FACE):
        box = proposal.value
        if all(k in box for k in ("x", "y", "w", "h")):
            x, y, w, h = rotate_box(box["x"], box["y"], box["w"], box["h"], width, height, degrees)
            proposal.value = {**box, "x": x, "y": y, "w": w, "h": h}
            proposal.save(update_fields=["value"])

    if degrees in (90, 270):
        photo.width, photo.height = height, width
    photo.rotation = (photo.rotation + degrees) % 360
    photo.pending_rotation = 0
    photo.phash = dedupe.dhash(rotated)
    photo.catalog_changed_at = timezone.now()  # re-export with the new orientation
    photo.save(
        update_fields=[
            "width",
            "height",
            "rotation",
            "pending_rotation",
            "phash",
            "catalog_changed_at",
        ]
    )


def apply_pending_rotations(limit=20):
    """Worker job. Returns how many photos were rotated."""
    done = 0
    pending = list(Photo.objects.exclude(pending_rotation=0).values_list("pk", flat=True)[:limit])
    for pk in pending:
        try:
            with transaction.atomic():
                photo = Photo.objects.select_for_update().get(pk=pk)
                if photo.pending_rotation % 360 == 0:
                    Photo.objects.filter(pk=pk).update(pending_rotation=0)
                    continue
                _apply(photo)
            done += 1
            logger.info("event=photo_rotated photo=%s rotation=%s", pk, photo.rotation)
        except Exception:
            logger.exception("event=photo_rotation_failed photo=%s", pk)
    return done
