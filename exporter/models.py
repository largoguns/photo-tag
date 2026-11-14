from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _


class ExportRequest(models.Model):
    """An export job requested from the web and executed by the worker."""

    class Status(models.TextChoices):
        PENDING = "pending", _("Pendiente")
        RUNNING = "running", _("En curso")
        DONE = "done", _("Completada")
        FAILED = "failed", _("Fallida")

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    requested_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    exported_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-requested_at", "-id"]

    def __str__(self):
        return gettext("Exportación %(id)s (%(status)s)") % {
            "id": self.pk,
            "status": self.get_status_display(),
        }
