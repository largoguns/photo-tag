import datetime

from django.conf import settings
from django.db import models
from django.utils import formats, timezone
from django.utils.translation import get_language, gettext
from django.utils.translation import gettext_lazy as _


class Scan(models.Model):
    sha256 = models.CharField(max_length=64, unique=True)
    file_name = models.CharField(_("fichero"), max_length=255)
    processed_at = models.DateTimeField(_("procesado"), default=timezone.now)
    needs_review = models.BooleanField(_("requiere revisión"), default=False, db_index=True)
    # Codes such as {"code": "photo_count", "count": 7}; translated when displayed.
    review_reasons = models.JSONField(default=list, blank=True)
    reviewed = models.BooleanField(_("revisado"), default=False)
    # Asked from the web; the worker re-runs the cropping (like process_scans --force).
    reprocess_requested = models.BooleanField(default=False)
    # Size of the original scan; filled when its preview for the crop editor is made.
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="uploaded_scans",
        verbose_name=_("subido por"),
    )

    class Meta:
        ordering = ["-processed_at", "-id"]

    def __str__(self):
        return self.file_name

    def review_reason_labels(self):
        return [review_reason_label(reason) for reason in self.review_reasons]


def review_reason_label(reason):
    if not isinstance(reason, dict):
        return str(reason)  # free text stored by older versions
    code = reason.get("code")
    if code == "photo_count":
        return gettext("Se han detectado %(count)s fotos") % {"count": reason.get("count")}
    if code == "aspect_ratio":
        return gettext("Relación de aspecto anómala")
    if code == "overlap":
        return gettext("Recortes solapados")
    if code == "orientation":
        return gettext("Orientación dudosa: comprueba si alguna foto está girada")
    if code == "split":
        return gettext("Fotos pegadas separadas automáticamente: comprueba los recortes")
    if code == "duplicate":
        return gettext("Posible duplicado de la foto %(photo)s") % {"photo": reason.get("photo")}
    return str(code)


class PhotoQuerySet(models.QuerySet):
    def visible(self):
        return self.filter(discarded=False)


class Photo(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", _("Pendiente")
        IN_PROGRESS = "in_progress", _("En curso")
        CATALOGED = "cataloged", _("Catalogada")

    class DatePrecision(models.TextChoices):
        EXACT = "exact", _("Fecha exacta")
        YEAR = "year", _("Año")
        DECADE = "decade", _("Década")

    scan = models.ForeignKey(Scan, on_delete=models.CASCADE, related_name="photos")
    file_name = models.CharField(_("fichero"), max_length=255)
    index = models.PositiveSmallIntegerField(_("índice"))
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    angle = models.FloatField(_("ángulo"), default=0)
    # Rotated rectangle in scan pixels, used to match photos when a scan is reprocessed.
    crop_rect = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    discarded = models.BooleanField(_("recorte descartado"), default=False, db_index=True)
    # Clockwise degrees (0/90/180/270) already applied to the file, and still to apply.
    rotation = models.PositiveSmallIntegerField(_("giro aplicado"), default=0)
    pending_rotation = models.PositiveSmallIntegerField(_("giro pendiente"), default=0)
    # Crop requested from the editor ({cx, cy, w, h, angle} in scan pixels), applied by
    # the worker. Once applied, crop_manual keeps it when the scan is reprocessed.
    pending_crop = models.JSONField(null=True, blank=True)
    crop_manual = models.BooleanField(_("recorte manual"), default=False)
    # Perspective crop: 4 scan points (TL, TR, BR, BL of the unturned photo), or None.
    crop_quad = models.JSONField(null=True, blank=True)
    # Bumped whenever the image file changes (rotation, crop): busts browser caches.
    image_version = models.PositiveIntegerField(default=0)
    # Perceptual hash (dHash, 64 bits as hex) to spot re-scans and recompressed copies.
    phash = models.CharField(max_length=16, blank=True, db_index=True)
    duplicate_of = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="possible_duplicates",
        verbose_name=_("posible duplicado de"),
    )

    # Accepted cataloging values (set only by the voting service).
    event_type = models.ForeignKey(
        "EventType", null=True, blank=True, on_delete=models.PROTECT, related_name="photos"
    )
    event_description = models.CharField(max_length=255, blank=True)
    date_precision = models.CharField(max_length=10, choices=DatePrecision.choices, blank=True)
    date_value = models.DateField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    catalog_changed_at = models.DateTimeField(null=True, blank=True)
    exported_at = models.DateTimeField(_("exportada en"), null=True, blank=True)
    export_path = models.CharField(max_length=255, blank=True)

    objects = PhotoQuerySet.as_manager()

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["scan", "index"], name="unique_photo_index_per_scan")
        ]

    def __str__(self):
        return gettext("Foto %(id)s") % {"id": self.pk}

    @property
    def decade(self):
        return self.date_value.year // 10 * 10 if self.date_value else None

    def date_label(self):
        if not self.date_value:
            return ""
        if self.date_precision == self.DatePrecision.DECADE:
            return gettext("Década de %(decade)s") % {"decade": self.decade}
        if self.date_precision == self.DatePrecision.YEAR:
            return str(self.date_value.year)
        return formats.date_format(self.date_value, "SHORT_DATE_FORMAT")

    def thumb_width(self, size):
        return min(size, self.width)


class PersonQuerySet(models.QuerySet):
    def active(self):
        return self.filter(merged_into__isnull=True)


class Person(models.Model):
    display_name = models.CharField(_("nombre a mostrar"), max_length=150, db_index=True)
    notes = models.TextField(_("notas"), blank=True)
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
    # Face chosen as main photo; when empty or no longer valid, the largest face is used.
    avatar_face = models.ForeignKey(
        "Face", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
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
        AUTO = "auto", _("Automática")
        MANUAL = "manual", _("Manual")

    class Status(models.TextChoices):
        ACTIVE = "active", _("Activa")
        PROPOSED = "proposed", _("Propuesta")  # drawn manually, add_face proposal still open
        REMOVED = "removed", _("Falso positivo")
        REJECTED = "rejected", _("Rechazada")

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
        return gettext("Cara %(id)s de %(photo)s") % {"id": self.pk, "photo": self.photo}

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
        return self.filter(active=True, is_provisional=False)


class EventType(models.Model):
    name = models.CharField(_("nombre"), max_length=100, unique=True)
    # Optional English name for the interface; XMP export always uses `name`.
    name_en = models.CharField(_("nombre en inglés"), max_length=100, blank=True)
    # Created by a user's "Other…" proposal; permanent once that proposal is accepted.
    is_provisional = models.BooleanField(_("pendiente de votación"), default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_event_types",
        verbose_name=_("propuesto por"),
    )
    active = models.BooleanField(_("activo"), default=True)
    order = models.PositiveSmallIntegerField(_("orden"), default=0)

    objects = EventTypeQuerySet.as_manager()

    class Meta:
        ordering = ["order", "name"]

    def __str__(self):
        return self.localized_name

    @property
    def localized_name(self):
        if self.name_en and (get_language() or "").startswith("en"):
            return self.name_en
        return self.name


class Proposal(models.Model):
    class Kind(models.TextChoices):
        FACE_IDENTITY = "face_identity", _("Identidad de cara")
        EVENT = "event", _("Evento")
        DATE = "date", _("Fecha")
        ADD_FACE = "add_face", _("Nueva cara")
        REMOVE_FACE = "remove_face", _("Falso positivo")

    class Status(models.TextChoices):
        OPEN = "open", _("Abierta")
        ACCEPTED = "accepted", _("Aceptada")
        REJECTED = "rejected", _("Rechazada")
        SUPERSEDED = "superseded", _("Sustituida")

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
        help_text=_("Administrador que forzó la decisión, si la hubo."),
    )

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.get_kind_display()} #{self.pk} ({self.get_status_display()})"

    @property
    def is_open(self):
        return self.status == self.Status.OPEN

    def label(self):
        """Human readable value in the active language."""
        if self.kind == self.Kind.FACE_IDENTITY:
            return self.person.display_name if self.person else gettext("(persona eliminada)")
        if self.kind == self.Kind.EVENT:
            name = str(self.event_type) if self.event_type else self.value.get("event_name", "?")
            description = self.value.get("description")
            return f"{name} — {description}" if description else name
        if self.kind == self.Kind.DATE:
            return date_label(self.value)
        if self.kind == self.Kind.ADD_FACE:
            return gettext("Añadir esta cara")
        if self.kind == self.Kind.REMOVE_FACE:
            return gettext("No es una cara")
        return ""


def date_label(value):
    precision = value.get("precision")
    if precision == Photo.DatePrecision.DECADE:
        return gettext("Década de %(decade)s") % {"decade": value.get("year")}
    if precision == Photo.DatePrecision.YEAR:
        return str(value.get("year"))
    if precision == Photo.DatePrecision.EXACT:
        day = datetime.date(value["year"], value["month"], value["day"])
        return formats.date_format(day, "SHORT_DATE_FORMAT")
    return ""


class Vote(models.Model):
    UP = 1
    DOWN = -1
    VALUE_CHOICES = [(UP, _("A favor")), (DOWN, _("En contra"))]

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
        return f"{self.user} {'+1' if self.value > 0 else '-1'} #{self.proposal_id}"


class CatalogSettings(models.Model):
    """Singleton with thresholds editable from the administration screen."""

    vote_min_approvals = models.PositiveSmallIntegerField(_("votos mínimos"), default=2)
    vote_approval_ratio = models.FloatField(_("proporción mínima a favor"), default=0.66)

    class Meta:
        verbose_name = _("ajustes")

    def __str__(self):
        return gettext("Ajustes del catálogo")

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


class Upload(models.Model):
    """An image uploaded from the web, waiting in the inbox for the worker."""

    class Kind(models.TextChoices):
        SCAN = "scan", _("Escaneo con varias fotos")
        SINGLE = "single", _("Foto suelta")

    class Status(models.TextChoices):
        PENDING = "pending", _("En cola")
        PROCESSED = "processed", _("Procesada")
        DUPLICATE = "duplicate", _("Ya estaba subida")
        FAILED = "failed", _("Error al procesar")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="uploads"
    )
    original_name = models.CharField(max_length=255)
    stored_name = models.CharField(max_length=255, unique=True)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.SCAN)
    sha256 = models.CharField(max_length=64, db_index=True)
    size = models.PositiveBigIntegerField()
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    scan = models.ForeignKey(
        Scan, null=True, blank=True, on_delete=models.SET_NULL, related_name="uploads"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return self.original_name


class FileCleanup(models.Model):
    """Files to remove (or move) after a deletion in the web.

    The web container cannot write photos, exports or scans: it deletes the database rows
    and leaves these tasks for the worker.
    """

    class Kind(models.TextChoices):
        PHOTO = "photo", "photo"  # photo JPG and its thumbnails, by file name
        EXPORT = "export", "export"  # exported JPG and .xmp, path relative to EXPORT_DIR
        PREVIEW = "preview", "preview"  # scan preview, by sha256
        ORIGINAL = "original", "original"  # scans/processed/<name> → scans/deleted/

    kind = models.CharField(max_length=10, choices=Kind.choices)
    name = models.CharField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.kind}:{self.name}"
