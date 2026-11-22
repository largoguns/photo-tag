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


class QuizAttempt(models.Model):
    """One family quiz (spec section 5.14): 10 questions with 4 options.

    `questions` keeps, per question, what is shown (text, images, options) and the index of
    the right option, which never reaches the browser before answering. `scored` is decided
    when it starts (only the first quizzes of each type per day give points).

    `timings` keeps, per question, when it was first shown and how long the answer took
    (measured on the server). `points` are the quiz points earned by the answers, without
    the perfect-score bonus: 1 per right answer, or 3/2/1 by speed in timed kinds.
    """

    class Kind(models.TextChoices):
        FACES = "faces", "faces"
        NICKNAMES = "nicknames", "nicknames"
        DATES = "dates", "dates"
        KINSHIP = "kinship", "kinship"
        LOCATIONS = "locations", "locations"
        HIDDEN = "hidden", "hidden"
        EMOJIS = "emojis", "emojis"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="quiz_attempts"
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    questions = models.JSONField(default=list)
    # Option index chosen for each question (a list of them when matching emojis).
    answers = models.JSONField(default=list)
    correct = models.PositiveSmallIntegerField(default=0)
    timings = models.JSONField(default=list)  # [{"shown": iso datetime, "seconds": float}]
    points = models.PositiveSmallIntegerField(default=0)
    scored = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.user} {self.kind} {self.correct}/{len(self.questions)}"

    @property
    def is_finished(self):
        return self.finished_at is not None
