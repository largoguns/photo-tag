from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class PushSubscription(models.Model):
    """One browser/device subscribed to Web Push for a user."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="push_subscriptions"
    )
    endpoint = models.URLField(max_length=1000, unique=True)
    p256dh = models.CharField(max_length=200)
    auth = models.CharField(max_length=100)
    # Language active when subscribing: notifications are written in it.
    language = models.CharField(max_length=10, default="es")
    device_label = models.CharField(_("dispositivo"), max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_success_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user} · {self.device_label or self.pk}"

    def as_subscription_info(self):
        return {"endpoint": self.endpoint, "keys": {"p256dh": self.p256dh, "auth": self.auth}}


class NotificationSettings(models.Model):
    """Which digests a user wants, and when each one was last sent."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_settings"
    )
    new_photos = models.BooleanField(_("Fotos nuevas para catalogar"), default=True)
    pending_votes = models.BooleanField(_("Propuestas pendientes de mi voto"), default=True)
    resolved = models.BooleanField(_("Mis propuestas aceptadas o rechazadas"), default=True)
    # Sent right away (no digest, no quiet hours): the user just asked for the scan.
    scans = models.BooleanField(_("Mis escaneos terminados"), default=True)
    # Start "now" so a new user is not notified about the whole history.
    new_photos_sent_at = models.DateTimeField(default=timezone.now)
    pending_votes_sent_at = models.DateTimeField(default=timezone.now)
    resolved_sent_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return str(self.user)

    @classmethod
    def for_user(cls, user):
        obj, _ = cls.objects.get_or_create(user=user)
        return obj
