"""Manual crop adjustment: requested from the web editor, applied by the worker.

Geometry: a photo is the rotated rectangle `crop_rect` of its scan ({cx, cy, w, h, angle},
OpenCV convention), shrunk by an inset and turned clockwise by `rotation` (0/90/180/270).
A scan point P and a point (u, v) relative to the rectangle centre are related by
P = c + R(angle)·(u, v). The editor works in "view" coordinates: pixels of the photo as it
is shown (already turned), and may extend beyond it to recover what was cut off.
"""

import logging
import math

from django.db import transaction
from django.utils import timezone

from accounts.services import audit
from catalog.models import Face, Photo, Proposal
from catalog.services.voting import refresh_photo_status
from scanner.services import cropping, dedupe, images, orientation, pipeline

logger = logging.getLogger("scanner")

MAX_STRAIGHTEN = 10.0  # degrees, either way, from the current angle
MIN_SIDE = 64  # photo pixels
BOUNDS_TOLERANCE = 2.0  # scan pixels


class CropError(ValueError):
    """The requested crop is not valid (too small, outside the scan, too rotated)."""


def _rect(photo, rect=None):
    rect = rect or photo.crop_rect
    return (rect["cx"], rect["cy"]), (rect["w"], rect["h"]), rect["angle"]


def unrotated_size(photo):
    """Width and height of the photo before its 90° turns."""
    if photo.rotation % 180:
        return photo.height, photo.width
    return photo.width, photo.height


def inset(photo):
    """Margin trimmed on each side after warping (0 for single photos)."""
    (_, _), (w, _), _ = _rect(photo)
    return max(0.0, (w - unrotated_size(photo)[0]) / 2)


def _unturn(x, y, rotation, width, height):
    """View point → point of the unturned photo. width/height: view size."""
    rotation %= 360
    if rotation == 90:
        return y, width - x
    if rotation == 180:
        return width - x, height - y
    if rotation == 270:
        return height - y, x
    return x, y


def _turn(x, y, rotation, width, height):
    """Point of the unturned photo → view point. width/height: unturned size."""
    rotation %= 360
    if rotation == 90:
        return height - y, x
    if rotation == 180:
        return width - x, height - y
    if rotation == 270:
        return y, width - x
    return x, y


def _rotate(u, v, degrees):
    r = math.radians(degrees)
    return u * math.cos(r) - v * math.sin(r), u * math.sin(r) + v * math.cos(r)


def view_to_scan(photo, x, y, angle=None, rect=None):
    """Scan point shown at view point (x, y) when the photo is straightened to `angle`."""
    (cx, cy), (w, h), current = _rect(photo, rect)
    angle = current if angle is None else angle
    ux, uy = _unturn(x, y, photo.rotation, photo.width, photo.height)
    margin = inset(photo)
    du, dv = _rotate(ux + margin - w / 2, uy + margin - h / 2, angle)
    return cx + du, cy + dv


def scan_to_view(rect, margin, rotation, X, Y):
    """View point of scan point (X, Y) for a photo cut from `rect` (crop_rect dict)."""
    cx, cy, w, h, angle = rect["cx"], rect["cy"], rect["w"], rect["h"], rect["angle"]
    u, v = _rotate(X - cx, Y - cy, -angle)
    width, height = w - 2 * margin, h - 2 * margin
    return _turn(u + w / 2 - margin, v + h / 2 - margin, rotation, width, height)


def new_rect(photo, box, angle):
    """Scan rectangle for the view box (x0, y0, x1, y1) straightened to `angle`."""
    x0, y0, x1, y1 = box
    (_, _), (_, _), current = _rect(photo)
    if abs(angle - current) > MAX_STRAIGHTEN + 0.01 or abs(angle) >= 45:
        raise CropError("Angle out of range")
    if x1 - x0 < MIN_SIDE or y1 - y0 < MIN_SIDE:
        raise CropError("Crop too small")
    cx, cy = view_to_scan(photo, (x0 + x1) / 2, (y0 + y1) / 2, angle)
    width, height = x1 - x0, y1 - y0
    if photo.rotation % 180:
        width, height = height, width
    margin = 2 * inset(photo)
    rect = {
        "cx": round(cx, 1),
        "cy": round(cy, 1),
        "w": round(width + margin, 1),
        "h": round(height + margin, 1),
        "angle": round(angle, 2),
    }
    _check_inside(photo.scan, rect, inset(photo))
    return rect


def _check_inside(scan, rect, margin):
    """The visible part (the rectangle minus the trimmed inset) must lie on the scan."""
    if not scan.width or not scan.height:
        return  # size unknown until the preview exists; the editor needs it anyway
    tolerance = BOUNDS_TOLERANCE
    visible = {**rect, "w": rect["w"] - 2 * margin, "h": rect["h"] - 2 * margin}
    for x, y in _corners(visible):
        if not (-tolerance <= x <= scan.width + tolerance):
            raise CropError("Crop outside the scan")
        if not (-tolerance <= y <= scan.height + tolerance):
            raise CropError("Crop outside the scan")


def _corners(rect):
    cx, cy, w, h, angle = rect["cx"], rect["cy"], rect["w"], rect["h"], rect["angle"]
    for u, v in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)):
        du, dv = _rotate(u, v, angle)
        yield cx + du, cy + dv


def request_crop(user, photo, box, angle):
    """Validate and queue a crop; the worker applies it in a few seconds."""
    rect = new_rect(photo, box, angle)
    Photo.objects.filter(pk=photo.pk).update(pending_crop=rect)
    photo.pending_crop = rect
    audit.log(user, "photo.crop", photo, rect=rect)
    return rect


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def cut(scan_image, rect, margin, rotation):
    """The photo for `rect`: deskewed, trimmed by `margin` and turned."""
    warped = cropping._warp(
        scan_image, ((rect["cx"], rect["cy"]), (rect["w"], rect["h"]), rect["angle"])
    )
    m = round(margin)
    height, width = warped.shape[:2]
    if m and height > 2 * m + 1 and width > 2 * m + 1:
        warped = warped[m : height - m, m : width - m]
    return orientation.rotate(warped, rotation)


def _move_box(photo, old_rect, new_rect_, margin, new_size, box):
    """Face box (x, y, w, h) in the old view → clipped box in the new view, or None."""
    x, y, w, h = box
    points = [
        scan_to_view(new_rect_, margin, photo.rotation, *view_to_scan(photo, px, py, rect=old_rect))
        for px, py in ((x, y), (x + w, y), (x + w, y + h), (x, y + h))
    ]
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    width, height = new_size
    x0, y0 = max(0, min(xs)), max(0, min(ys))
    x1, y1 = min(width, max(xs)), min(height, max(ys))
    if x1 - x0 < 1 or y1 - y0 < 1:
        return None
    return round(x0), round(y0), max(1, round(x1 - x0)), max(1, round(y1 - y0))


def _touched(face):
    return bool(face.person_id or face.source == Face.Source.MANUAL or face.proposals.exists())


def _apply(photo, scan_image):
    old_rect = dict(photo.crop_rect)
    rect = photo.pending_crop
    margin = inset(photo)
    image = cut(scan_image, rect, margin, photo.rotation)
    images.save_photo(image, photo.file_name)
    images.make_thumbnails(photo.file_name)
    new_size = (image.shape[1], image.shape[0])

    # Faces with human input follow the photo; automatic ones are detected again.
    kept = []
    for face in Face.objects.filter(photo=photo):
        if not _touched(face):
            face.delete()
            continue
        moved = _move_box(photo, old_rect, rect, margin, new_size, (face.x, face.y, face.w, face.h))
        if moved is None:  # cut off entirely: keep it (it has votes) as a tiny box at the edge
            moved = (min(face.x, new_size[0] - 1), min(face.y, new_size[1] - 1), 1, 1)
        face.x, face.y, face.w, face.h = moved
        face.embedding = None
        face.save(update_fields=["x", "y", "w", "h", "embedding"])
        kept.append(moved)
    for proposal in Proposal.objects.filter(photo=photo, kind=Proposal.Kind.ADD_FACE):
        box = proposal.value
        if all(k in box for k in ("x", "y", "w", "h")):
            moved = _move_box(
                photo, old_rect, rect, margin, new_size, (box["x"], box["y"], box["w"], box["h"])
            )
            if moved:
                x, y, w, h = moved
                proposal.value = {**box, "x": x, "y": y, "w": w, "h": h}
                proposal.save(update_fields=["value"])
    detected = pipeline._detect(image)
    fresh = [
        f
        for f in detected
        if all(pipeline._iou((f.x, f.y, f.w, f.h), k) < pipeline.FACE_DUPLICATE_IOU for k in kept)
    ]
    pipeline._create_faces(photo, fresh)

    photo.width, photo.height = new_size
    photo.crop_rect, photo.angle = rect, rect["angle"]
    photo.pending_crop, photo.crop_manual = None, True
    photo.image_version += 1
    photo.phash = dedupe.dhash(image)
    photo.catalog_changed_at = timezone.now()  # re-export the new crop
    photo.save(
        update_fields=[
            "width",
            "height",
            "crop_rect",
            "angle",
            "pending_crop",
            "crop_manual",
            "image_version",
            "phash",
            "catalog_changed_at",
        ]
    )
    refresh_photo_status(photo)
    return image


def apply_pending_crops(limit=20):
    """Worker job. Returns how many photos were re-cropped."""
    done = 0
    pending = Photo.objects.filter(pending_crop__isnull=False).values_list("pk", flat=True)
    for pk in list(pending[:limit]):
        try:
            with transaction.atomic():
                photo = Photo.objects.select_for_update().select_related("scan").get(pk=pk)
                if not photo.pending_crop:
                    continue
                source = pipeline._source_file(photo.scan)
                image = _apply(photo, pipeline.decode_image(source.read_bytes()))
            pipeline._embed_photos([(image, photo)])
            done += 1
            logger.info("event=photo_cropped photo=%s rect=%s", pk, photo.crop_rect)
        except Exception:
            logger.exception("event=photo_crop_failed photo=%s", pk)
            Photo.objects.filter(pk=pk).update(pending_crop=None)  # do not retry forever
    return done


def make_missing_previews(limit=20):
    """Worker job: preview (and size) of scans processed before the crop editor existed."""
    from catalog.models import Scan

    done = 0
    for scan in Scan.objects.filter(width__isnull=True)[:limit]:
        try:
            image = pipeline.decode_image(pipeline._source_file(scan).read_bytes())
        except Exception:
            logger.warning("event=preview_skipped scan=%s reason=source_missing", scan.sha256[:12])
            Scan.objects.filter(pk=scan.pk).update(width=0, height=0)  # do not retry
            continue
        images.save_preview(image, scan.sha256)
        Scan.objects.filter(pk=scan.pk).update(width=image.shape[1], height=image.shape[0])
        done += 1
    return done


def editor_ready(photo):
    scan = photo.scan
    return (
        bool(scan.width)
        and images.preview_path(scan.sha256).exists()
        and bool(photo.crop_rect.get("w"))
    )
