"""Scan from a network scanner (eSCL/AirScan) with SANE, driven from the web.

The worker runs `scanimage` against the printer configured in SCANNER_URL and drops a
lossless PNG in the inbox together with an Upload record, so the regular upload flow
(cropping, faces, duplicates, statuses) takes over.
"""

import hashlib
import logging
import os
import subprocess
import time
import uuid
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from accounts.services import audit
from catalog.models import Upload
from scanner.models import ScanJob

logger = logging.getLogger("scanner")

DEVICE_NAME = "PhotoTag"
DEVICE = f"airscan:e0:{DEVICE_NAME}"
# Printers waking up from sleep often refuse the first eSCL connections ("open of device
# ... failed"). Nothing has been scanned yet, so it is safe to wait and try again.
OPEN_RETRY_DELAYS = (3, 6, 10)


class ScannerError(Exception):
    """A failure with a message the user can act on."""


def is_configured():
    return bool(settings.SCANNER_URL)


def sane_config_dir():
    """Write the SANE configuration (only the airscan backend, the printer by URL).

    mDNS discovery does not cross the Docker network, so the device is declared by URL and
    discovery is disabled. /etc/sane.d is not writable for the app user.
    """
    directory = Path(settings.SANE_CONFIG_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "dll.conf").write_text("airscan\n")
    (directory / "airscan.conf").write_text(
        "[devices]\n"
        f'"{DEVICE_NAME}" = {settings.SCANNER_URL}, eSCL\n'
        "\n[options]\n"
        "discovery = disable\n"
    )
    return directory


def run_scanimage(args, timeout):
    env = {**os.environ, "SANE_CONFIG_DIR": str(sane_config_dir())}
    return subprocess.run(
        ["scanimage", *args], capture_output=True, timeout=timeout, env=env, check=False
    )


def explain(stderr):
    """Translate common scanimage errors into something a family member understands."""
    text = stderr.lower()
    if "unrecognized option" in text:
        return _(
            "El software de escaneo del servidor no admite una opción. Revisa los logs del worker."
        )
    if "invalid argument" in text or "not found" in text or "no such device" in text:
        return _(
            "No se encuentra el escáner: revisa SCANNER_URL y que la impresora esté en la red."
        )
    if "busy" in text:
        return _("El escáner está ocupado. Espera a que termine y vuelve a intentarlo.")
    if "jammed" in text or "cover" in text or "no documents" in text:
        return _("Revisa el escáner: la tapa, el papel o el cristal.")
    return _("La impresora no responde: ¿está encendida y conectada a la WiFi?")


_option_support = {}


def photo_intent_args():
    """`--scan-intent Photo` (optimizes for photographs) when this sane-airscan has it.

    Older sane-airscan releases (e.g. Ubuntu's) reject the option, so ask the device's
    option list once per scanner URL and remember the answer.
    """
    url = settings.SCANNER_URL
    if url not in _option_support:
        try:
            result = run_scanimage(["-d", DEVICE, "-A"], timeout=60)
        except (OSError, subprocess.SubprocessError):
            return []  # unknown for now; asked again on the next scan
        if result.returncode != 0:
            return []  # printer not answering (asleep?): do not remember a wrong answer
        _option_support[url] = "--scan-intent" in result.stdout.decode(errors="replace")
    return ["--scan-intent", "Photo"] if _option_support[url] else []


def is_open_failure(result):
    """The device could not be opened: the scan did not even start."""
    return result.returncode != 0 and b"open of device" in result.stderr


def scan_to_file(resolution, target, before_publish=None):
    """Scan one page as PNG into `target` (written atomically).

    `before_publish(partial_path)` runs before the file appears in the inbox, so the
    Upload record exists by the time the worker picks the file up.
    """
    partial = target.with_name(f".{target.name}.part")
    try:
        for delay in (*OPEN_RETRY_DELAYS, None):
            result = run_scanimage(
                [
                    "-d",
                    DEVICE,
                    "--mode",
                    "Color",
                    *photo_intent_args(),
                    "--resolution",
                    str(resolution),
                    "--format=png",
                    "-o",
                    str(partial),
                ],
                timeout=settings.SCAN_TIMEOUT_SECONDS,
            )
            if delay is None or not is_open_failure(result):
                break
            logger.info("event=scan_open_retry delay=%ss", delay)
            partial.unlink(missing_ok=True)
            time.sleep(delay)
    except subprocess.TimeoutExpired as exc:
        partial.unlink(missing_ok=True)
        raise ScannerError(
            _("El escaneo ha tardado demasiado. ¿Sigue encendida la impresora?")
        ) from exc
    except FileNotFoundError as exc:  # scanimage missing (outside the Docker image)
        raise ScannerError(_("Este servidor no tiene instalado el software de escaneo.")) from exc
    stderr = result.stderr.decode(errors="replace").strip()
    if result.returncode != 0 or not partial.exists() or partial.stat().st_size == 0:
        partial.unlink(missing_ok=True)
        logger.warning("event=scan_failed code=%s stderr=%s", result.returncode, stderr[-500:])
        raise ScannerError(explain(stderr))
    if before_publish is not None:
        before_publish(partial)
    partial.replace(target)
    return target


def request_scan(user, resolution):
    if resolution not in settings.SCAN_RESOLUTIONS:
        raise ValueError("Unsupported resolution")
    job = ScanJob.objects.create(requested_by=user, resolution=resolution)
    audit.log(user, "scan.request", job, resolution=resolution)
    return job


def run_pending_scan():
    """Worker job: run the oldest queued scan (the scanner handles one at a time)."""
    if not is_configured():
        return None
    with transaction.atomic():
        job = (
            ScanJob.objects.select_for_update(skip_locked=True)
            .filter(status=ScanJob.Status.QUEUED)
            .order_by("created_at", "pk")
            .first()
        )
        if job is None or ScanJob.objects.filter(status=ScanJob.Status.SCANNING).exists():
            return None
        job.status, job.started_at = ScanJob.Status.SCANNING, timezone.now()
        job.save(update_fields=["status", "started_at"])

    stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
    target = Path(settings.SCANS_INBOX_DIR) / f"scan-{stamp}-{uuid.uuid4().hex[:8]}.png"
    target.parent.mkdir(parents=True, exist_ok=True)

    def register_upload(partial):
        job.upload = Upload.objects.create(
            user=job.requested_by,
            original_name=target.name,
            stored_name=target.name,
            kind=Upload.Kind.SCAN,
            sha256=hashlib.sha256(partial.read_bytes()).hexdigest(),
            size=partial.stat().st_size,
        )

    try:
        scan_to_file(job.resolution, target, before_publish=register_upload)
    except ScannerError as exc:
        job.status, job.error = ScanJob.Status.FAILED, str(exc)
    except Exception:  # never leave a job stuck in "scanning"
        logger.exception("event=scan_crashed job=%s", job.pk)
        job.status, job.error = ScanJob.Status.FAILED, _("Error inesperado al escanear.")
        target.with_name(f".{target.name}.part").unlink(missing_ok=True)
    else:
        job.status = ScanJob.Status.DONE
        logger.info("event=scan_done job=%s file=%s", job.pk, target.name)
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "error", "upload", "finished_at"])
    if job.status == ScanJob.Status.FAILED:
        # Success is notified later, once the pipeline has cropped the photos.
        from notifications.services.scans import notify

        notify(job, "failed")
    return job


def check():
    """Diagnostics for `manage.py scanner_check`: device list and device options."""
    listing = run_scanimage(["-L"], timeout=60)
    options = run_scanimage(["-d", DEVICE, "-A"], timeout=60)
    return listing, options
