from django.conf import settings
from django.db import models
from django.utils import timezone


class Scan(models.Model):
    sha256 = models.CharField(max_length=64, unique=True)
    file_name = models.CharField("fichero", max_length=255)
    processed_at = models.DateTimeField("procesado", default=timezone.now)
    needs_review = models.BooleanField("requiere revisión", default=False, db_index=True)
    review_reasons = models.JSONField(default=list, blank=True)
    reviewed = models.BooleanField("revisado", default=False)

    class Meta:
        ordering = ["-processed_at", "-id"]

    def __str__(self):
        return self.file_name


class PhotoQuerySet(models.QuerySet):
    def visible(self):
        return self.filter(discarded=False)


class Photo(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pendiente"
        IN_PROGRESS = "in_progress", "En curso"
        CATALOGED = "cataloged", "Catalogada"

    class DatePrecision(models.TextChoices):
        EXACT = "exact", "Fecha exacta"
        YEAR = "year", "Año"
        DECADE = "decade", "Década"

    scan = models.ForeignKey(Scan, on_delete=models.CASCADE, related_name="photos")
    file_name = models.CharField("fichero", max_length=255)
    index = models.PositiveSmallIntegerField("índice")
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    angle = models.FloatField("ángulo", default=0)
    # Rotated rectangle in scan pixels, used to match photos when a scan is reprocessed.
    crop_rect = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    discarded = models.BooleanField("recorte descartado", default=False, db_index=True)

    # Accepted cataloging values (set only by the voting service).
    event_type = models.ForeignKey(
        "EventType", null=True, blank=True, on_delete=models.PROTECT, related_name="photos"
    )
    event_description = models.CharField(max_length=255, blank=True)
    date_precision = models.CharField(max_length=10, choices=DatePrecision.choices, blank=True)
    date_value = models.DateField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    catalog_changed_at = models.DateTimeField(null=True, blank=True)
    exported_at = models.DateTimeField("exportada en", null=True, blank=True)
    export_path = models.CharField(max_length=255, blank=True)

    objects = PhotoQuerySet.as_manager()

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["scan", "index"], name="unique_photo_index_per_scan")
        ]

    def __str__(self):
        return f"Foto {self.pk}"

    @property
    def decade(self):
        return self.date_value.year // 10 * 10 if self.date_value else None

    def date_label(self):
        if not self.date_value:
            return ""
        if self.date_precision == self.DatePrecision.DECADE:
            return f"Década de {self.decade}"
        if self.date_precision == self.DatePrecision.YEAR:
            return str(self.date_value.year)
        return self.date_value.strftime("%d/%m/%Y")

    def thumb_width(self, size):
        return min(size, self.width)


class PersonQuerySet(models.QuerySet):
    def active(self):
        return self.filter(merged_into__isnull=True)


class Person(models.Model):
    display_name = models.CharField("nombre a mostrar", max_length=150, db_index=True)
    notes = models.TextField("notas", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_persons",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    # True while the person only exists because of an open proposal.
    is_provisional = models.BooleanField(default=False)
    merged_into = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="merged_persons"
    )
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="person",
    )

    objects = PersonQuerySet.as_manager()

    class Meta:
        ordering = ["display_name", "id"]

    def __str__(self):
        return self.display_name

    def resolved(self):
        """Follow merges to the surviving person."""
        person = self
        seen = set()
        while person.merged_into_id and person.pk not in seen:
            seen.add(person.pk)
            person = person.merged_into
        return person


class Face(models.Model):
    class Source(models.TextChoices):
        AUTO = "auto", "Automática"
        MANUAL = "manual", "Manual"

    class Status(models.TextChoices):
        ACTIVE = "active", "Activa"
        PROPOSED = "proposed", "Propuesta"  # drawn manually, add_face proposal still open
        REMOVED = "removed", "Falso positivo"
        REJECTED = "rejected", "Rechazada"

    photo = models.ForeignKey(Photo, on_delete=models.CASCADE, related_name="faces")
    x = models.PositiveIntegerField()
    y = models.PositiveIntegerField()
    w = models.PositiveIntegerField()
    h = models.PositiveIntegerField()
    score = models.FloatField(null=True, blank=True)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.AUTO)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.ACTIVE, db_index=True
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_faces",
    )
    person = models.ForeignKey(
        Person, null=True, blank=True, on_delete=models.SET_NULL, related_name="faces"
    )
    # SFace feature vector (128 float32, L2-normalized); computed by the worker.
    embedding = models.BinaryField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["photo", "x", "id"]

    def __str__(self):
        return f"Cara {self.pk} de {self.photo}"

    @property
    def is_visible(self):
        return self.status in (self.Status.ACTIVE, self.Status.PROPOSED)

    def as_percent(self):
        """Box as percentages of the photo size, for CSS overlays."""
        pw, ph = self.photo.width, self.photo.height
        return {
            "left": 100 * self.x / pw,
            "top": 100 * self.y / ph,
            "width": 100 * self.w / pw,
            "height": 100 * self.h / ph,
        }


class EventTypeQuerySet(models.QuerySet):
    def enabled(self):
        return self.filter(active=True)


class EventType(models.Model):
    name = models.CharField("nombre", max_length=100, unique=True)
    active = models.BooleanField("activo", default=True)
    order = models.PositiveSmallIntegerField("orden", default=0)

    objects = EventTypeQuerySet.as_manager()

    class Meta:
        ordering = ["order", "name"]

    def __str__(self):
        return self.name


class Proposal(models.Model):
    class Kind(models.TextChoices):
        FACE_IDENTITY = "face_identity", "Identidad de cara"
        EVENT = "event", "Evento"
        DATE = "date", "Fecha"
        ADD_FACE = "add_face", "Nueva cara"
        REMOVE_FACE = "remove_face", "Falso positivo"

    class Status(models.TextChoices):
        OPEN = "open", "Abierta"
        ACCEPTED = "accepted", "Aceptada"
        REJECTED = "rejected", "Rechazada"
        SUPERSEDED = "superseded", "Sustituida"

    photo = models.ForeignKey(Photo, on_delete=models.CASCADE, related_name="proposals")
    face = models.ForeignKey(
        Face, null=True, blank=True, on_delete=models.CASCADE, related_name="proposals"
    )
    kind = models.CharField(max_length=20, choices=Kind.choices, db_index=True)
    # Kind-specific payload (event description, date parts, face box...).
    value = models.JSONField(default=dict, blank=True)
    person = models.ForeignKey(
        Person, null=True, blank=True, on_delete=models.SET_NULL, related_name="proposals"
    )
    event_type = models.ForeignKey(
        EventType, null=True, blank=True, on_delete=models.PROTECT, related_name="proposals"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="proposals"
    )
    created_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.OPEN, db_index=True
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="forced_decisions",
        help_text="Administrador que forzó la decisión, si la hubo.",
    )

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.get_kind_display()} #{self.pk} ({self.get_status_display()})"

    @property
    def is_open(self):
        return self.status == self.Status.OPEN

    def label(self):
        """Human readable value, in Spanish."""
        if self.kind == self.Kind.FACE_IDENTITY:
            return self.person.display_name if self.person else "(persona eliminada)"
        if self.kind == self.Kind.EVENT:
            name = self.event_type.name if self.event_type else "?"
            description = self.value.get("description")
            return f"{name} — {description}" if description else name
        if self.kind == self.Kind.DATE:
            return date_label(self.value)
        if self.kind == self.Kind.ADD_FACE:
            return "Añadir esta cara"
        if self.kind == self.Kind.REMOVE_FACE:
            return "No es una cara"
        return ""


def date_label(value):
    precision = value.get("precision")
    if precision == Photo.DatePrecision.DECADE:
        return f"Década de {value.get('year')}"
    if precision == Photo.DatePrecision.YEAR:
        return str(value.get("year"))
    if precision == Photo.DatePrecision.EXACT:
        return f"{value.get('day'):02d}/{value.get('month'):02d}/{value.get('year')}"
    return ""


class Vote(models.Model):
    UP = 1
    DOWN = -1
    VALUE_CHOICES = [(UP, "A favor"), (DOWN, "En contra")]

    proposal = models.ForeignKey(Proposal, on_delete=models.CASCADE, related_name="votes")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="votes"
    )
    value = models.SmallIntegerField(choices=VALUE_CHOICES)
    created_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["proposal", "user"], name="unique_vote_per_user")
        ]

    def __str__(self):
        return f"{self.user} {'+1' if self.value > 0 else '-1'} en {self.proposal_id}"


class CatalogSettings(models.Model):
    """Singleton with thresholds editable from the administration screen."""

    vote_min_approvals = models.PositiveSmallIntegerField("votos mínimos", default=2)
    vote_approval_ratio = models.FloatField("proporción mínima a favor", default=0.66)

    class Meta:
        verbose_name = "ajustes"

    def __str__(self):
        return "Ajustes del catálogo"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(
            pk=1,
            defaults={
                "vote_min_approvals": settings.VOTE_MIN_APPROVALS,
                "vote_approval_ratio": settings.VOTE_APPROVAL_RATIO,
            },
        )
        return obj
