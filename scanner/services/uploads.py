"""Images uploaded from the web: validated and dropped into the scans inbox for the worker."""

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils.translation import gettext as _
from PIL import Image, UnidentifiedImageError

from accounts.services import audit
from catalog.models import Scan, Upload

# Pillow format → file extension the pipeline accepts.
ALLOWED_FORMATS = {"JPEG": ".jpg", "MPO": ".jpg", "PNG": ".png", "TIFF": ".tif"}
CHUNK = 1024 * 1024


@dataclass
class UploadResult:
    name: str
    ok: bool
    message: str
    upload: Upload | None = None


def _detect_format(uploaded):
    """Real image format from the content (not the name), or None."""
    uploaded.seek(0)
    try:
        with Image.open(uploaded) as image:
            image_format = image.format
            image.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        return None
    finally:
        uploaded.seek(0)
    return image_format if image_format in ALLOWED_FORMATS else None


def _sha256(uploaded):
    digest = hashlib.sha256()
    uploaded.seek(0)
    for chunk in uploaded.chunks(CHUNK):
        digest.update(chunk)
    uploaded.seek(0)
    return digest.hexdigest()


def _write_atomically(uploaded, target):
    """Write as .part first: the worker only picks up image extensions."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.part")
    with partial.open("wb") as handle:
        for chunk in uploaded.chunks(CHUNK):
            handle.write(chunk)
    partial.replace(target)


def save_upload(user, uploaded, kind):
    """Validate one uploaded file and queue it for processing."""
    name = Path(uploaded.name or "imagen").name[:255]
    if kind not in Upload.Kind.values:
        return UploadResult(name, False, _("Tipo de subida no válido."))
    if uploaded.size > settings.UPLOAD_MAX_MB * 1024 * 1024:
        return UploadResult(
            name, False, _("Supera el tamaño máximo de %(mb)s MB.") % {"mb": settings.UPLOAD_MAX_MB}
        )
    image_format = _detect_format(uploaded)
    if image_format is None:
        return UploadResult(name, False, _("No es una imagen JPG, PNG o TIFF válida."))

    sha = _sha256(uploaded)
    if (
        Scan.objects.filter(sha256=sha).exists()
        or Upload.objects.filter(sha256=sha, status=Upload.Status.PENDING).exists()
    ):
        return UploadResult(name, False, _("Esta imagen ya se había subido."))

    stored = f"upload-{uuid.uuid4().hex}{ALLOWED_FORMATS[image_format]}"
    _write_atomically(uploaded, Path(settings.SCANS_INBOX_DIR) / stored)
    with transaction.atomic():
        upload = Upload.objects.create(
            user=user,
            original_name=name,
            stored_name=stored,
            kind=kind,
            sha256=sha,
            size=uploaded.size,
        )
        audit.log(user, "upload.create", upload, name=name, kind=kind)
    return UploadResult(name, True, _("En cola para procesar."), upload)


def save_uploads(user, files, kind):
    files = list(files)[: settings.UPLOAD_MAX_FILES]
    return [save_upload(user, uploaded, kind) for uploaded in files]


def pending_upload_for(sha):
    return (
        Upload.objects.filter(sha256=sha, status=Upload.Status.PENDING)
        .order_by("created_at")
        .first()
    )
