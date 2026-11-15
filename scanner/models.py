# Scan, Photo and Face live in the catalog app; the scanner app adds the scan jobs that
# drive a network scanner from the web.
from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class ScanJob(models.Model):
    """A scan requested from the web and executed by the worker on the network scanner."""

    class Status(models.TextChoices):
        QUEUED = "queued", _("En cola")
        SCANNING = "scanning", _("Escaneando")
        DONE = "done", _("Enviado al procesado")
        FAILED = "failed", _("Error")

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="scan_jobs"
    )
    resolution = models.PositiveSmallIntegerField(_("resolución"))
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.QUEUED, db_index=True
    )
    error = models.TextField(blank=True)
    upload = models.ForeignKey(
        "catalog.Upload", null=True, blank=True, on_delete=models.SET_NULL, related_name="scan_jobs"
    )
    created_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"ScanJob {self.pk} ({self.status})"

    @property
    def is_active(self):
        return self.status in (self.Status.QUEUED, self.Status.SCANNING)
