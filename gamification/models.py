from django.conf import settings
from django.db import models
from django.utils import timezone


class ScoreEvent(models.Model):
    """Points earned by a collaborator for something that turned out right.

    Unique per (user, reason, reference): nothing scores twice. Revoking deletes the row.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="score_events"
    )
    reason = models.CharField(max_length=40, db_index=True)
    points = models.IntegerField()
    ref_type = models.CharField(max_length=40, blank=True)
    ref_id = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "reason", "ref_type", "ref_id"], name="unique_score_event"
            )
        ]

    def __str__(self):
        return f"{self.user} {self.reason} {self.points:+d}"


class Achievement(models.Model):
    """A badge unlocked by a collaborator (recorded so it is announced only once)."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="achievements"
    )
    code = models.CharField(max_length=40)
    unlocked_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["unlocked_at", "id"]
        constraints = [models.UniqueConstraint(fields=["user", "code"], name="unique_achievement")]

    def __str__(self):
        return f"{self.user} {self.code}"
