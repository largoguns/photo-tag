"""Immediate push notification when a scan requested from the web finishes.

Unlike the digests, these are sent right away and ignore quiet hours: the user has just
asked for the scan and is waiting for it.
"""

import logging

from django.urls import reverse
from django.utils import formats, timezone, translation
from django.utils.translation import gettext, ngettext

from catalog.models import Upload
from notifications.models import NotificationSettings, PushSubscription
from notifications.services import push

logger = logging.getLogger("notifications")

TAG = "phototag-scan"


def _payload(job, outcome):
    """Title/body/url in the active language. `outcome`: processed, duplicate or failed."""
    when = formats.time_format(timezone.localtime(job.created_at), "TIME_FORMAT")
    if outcome == "processed":
        photos = list(job.upload.scan.photos.filter(discarded=False).order_by("index"))
        count = len(photos)
        body = ngettext(
            "%(count)s foto lista del escaneo de las %(time)s",
            "%(count)s fotos listas del escaneo de las %(time)s",
            count,
        ) % {"count": count, "time": when}
        url = reverse("photo_detail", args=[photos[0].pk]) if photos else reverse("scan")
    elif outcome == "duplicate":
        body = gettext("El escaneo de las %(time)s ya estaba en PhotoTag.") % {"time": when}
        url = reverse("scan")
    else:
        body = gettext("No se ha podido escanear: %(reason)s") % {
            "reason": job.error or gettext("error al procesar la imagen.")
        }
        url = reverse("scan")
    return {"title": "PhotoTag", "body": body, "url": url, "tag": TAG}


def notify(job, outcome):
    """Send the result of `job` to the devices of whoever requested it. Returns how many."""
    user = job.requested_by
    if user is None or not push.is_enabled():
        return 0
    if not NotificationSettings.for_user(user).scans:
        return 0
    sent = 0
    for subscription in PushSubscription.objects.filter(user=user):
        try:
            with translation.override(subscription.language):
                payload = _payload(job, outcome)
            sent += push.send(subscription, payload)
        except Exception:  # a notification must never break scanning or processing
            logger.exception("event=scan_notification_failed job=%s", job.pk)
    return sent


def notify_upload_finished(upload):
    """Called by the pipeline once an upload is processed (or rejected/failed)."""
    from scanner.models import ScanJob

    job = ScanJob.objects.filter(upload=upload).select_related("upload__scan").first()
    if job is None:
        return 0  # a regular web upload, not a scan from the scanner screen
    upload.refresh_from_db()
    outcome = {
        Upload.Status.PROCESSED: "processed",
        Upload.Status.DUPLICATE: "duplicate",
    }.get(upload.status, "failed")
    job.upload = upload
    return notify(job, outcome)
