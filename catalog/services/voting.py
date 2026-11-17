"""Proposal and voting rules (spec section 5.4).

All cataloging changes go through this module: views only collect input and call it.
"""

import datetime

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from accounts.services import audit
from catalog.models import (
    CatalogSettings,
    EventType,
    Face,
    Location,
    Person,
    Photo,
    Proposal,
    Vote,
)
from catalog.services.names import name_key, normalize_label, normalize_name

Kind = Proposal.Kind
Status = Proposal.Status


# ---------------------------------------------------------------------------
# Proposal creation
# ---------------------------------------------------------------------------


def resolve_person(name, user):
    """Return an existing active person by name, or a new provisional one."""
    name = normalize_name(name)
    if not name:
        raise ValidationError(_("El nombre no puede estar vacío."))
    # Accent- and case-insensitive: "adri", "Adri" and "ADRÍ" are the same person.
    key = name_key(name)
    for person in Person.objects.active().order_by("id").only("id", "display_name"):
        if name_key(person.display_name) == key:
            return Person.objects.get(pk=person.pk)
    return Person.objects.create(display_name=name, created_by=user, is_provisional=True)


def resolve_event_type(name, user):
    """Existing event type by name (any language, ignoring accents) or a new provisional one."""
    label = normalize_label(name)[:100]
    if not label:
        raise ValidationError(_("Escribe qué evento es."))
    key = name_key(label)
    for event_type in EventType.objects.order_by("-active", "id"):
        if key in (name_key(event_type.name), name_key(event_type.name_en)):
            return event_type
    return EventType.objects.create(name=label, created_by=user, is_provisional=True, order=900)


def resolve_location(name, user):
    """Existing location by name (ignoring accents and case) or a new provisional one.

    The typed text is kept as written, only trimmed: "Casa de Los Tarantos".
    """
    text = " ".join(str(name).split())[:150]
    if not text:
        raise ValidationError(_("Escribe dónde se hizo la foto."))
    existing = Location.objects.filter(name_key=name_key(text)).first()
    if existing:
        return existing.resolved()
    return Location.objects.create(
        name=text, name_key=name_key(text), created_by=user, is_provisional=True
    )


def normalize_date(precision, year, month=None, day=None):
    """Validate a date proposal and return its JSON value."""
    try:
        year = int(year)
    except (TypeError, ValueError) as exc:
        raise ValidationError(_("Año no válido.")) from exc
    if not 1800 <= year <= timezone.now().year:
        raise ValidationError(_("Año fuera de rango."))
    if precision == Photo.DatePrecision.DECADE:
        return {"precision": precision, "year": year // 10 * 10}
    if precision == Photo.DatePrecision.YEAR:
        return {"precision": precision, "year": year}
    if precision == Photo.DatePrecision.EXACT:
        try:
            datetime.date(year, int(month), int(day))
        except (TypeError, ValueError) as exc:
            raise ValidationError(_("Fecha no válida.")) from exc
        return {"precision": precision, "year": year, "month": int(month), "day": int(day)}
    raise ValidationError(_("Precisión de fecha no válida."))


def _same_field(proposal):
    """Proposals competing for the same catalog field as `proposal`."""
    qs = Proposal.objects.filter(photo_id=proposal.photo_id, kind=proposal.kind)
    if proposal.kind in (Kind.FACE_IDENTITY, Kind.REMOVE_FACE):
        return qs.filter(face_id=proposal.face_id)
    if proposal.kind == Kind.ADD_FACE:
        return qs.filter(pk=proposal.pk)
    return qs


def _find_duplicate(photo, kind, face, person, event_type, value, location=None):
    qs = Proposal.objects.filter(photo=photo, kind=kind, face=face, status=Status.OPEN)
    if kind == Kind.FACE_IDENTITY:
        return qs.filter(person=person).first()
    if kind == Kind.EVENT:
        description = value.get("description", "")
        return next(
            (
                p
                for p in qs.filter(event_type=event_type)
                if p.value.get("description", "") == description
            ),
            None,
        )
    if kind == Kind.DATE:
        return next((p for p in qs if p.value == value), None)
    if kind == Kind.LOCATION:
        return qs.filter(location=location).first()
    if kind == Kind.REMOVE_FACE:
        return qs.first()
    return None


@transaction.atomic
def create_proposal(
    user,
    photo,
    kind,
    *,
    face=None,
    person=None,
    person_name=None,
    event_type=None,
    event_name=None,
    location=None,
    location_name=None,
    value=None,
):
    """Create a proposal (counting as the author's +1) and evaluate it.

    If an identical open proposal already exists, the user's +1 is added to it instead.
    """
    value = dict(value or {})
    photo = Photo.objects.select_for_update().get(pk=photo.pk)

    if face is not None:
        face = Face.objects.select_related("person").get(pk=face.pk)
    if kind in (Kind.FACE_IDENTITY, Kind.REMOVE_FACE):
        if face is None or face.photo_id != photo.pk or not face.is_visible:
            raise ValidationError(_("Cara no válida."))
        if kind == Kind.FACE_IDENTITY:
            if person is None:
                person = resolve_person(person_name or "", user)
            person = person.resolved()
            if face.person_id == person.pk:
                raise ValidationError(_("Esa persona ya está aceptada para esta cara."))
    elif kind == Kind.EVENT:
        if event_name and event_name.strip():
            event_type = resolve_event_type(event_name, user)
        if event_type is None:
            raise ValidationError(_("Falta el tipo de evento."))
        value = {
            "description": " ".join(value.get("description", "").split())[:255],
            # Kept so the proposal still reads well if a provisional type is removed.
            "event_name": event_type.name,
        }
        if photo.event_type_id == event_type.pk and photo.event_description == value["description"]:
            raise ValidationError(_("Ese evento ya está aceptado."))
    elif kind == Kind.DATE:
        value = normalize_date(
            value.get("precision"), value.get("year"), value.get("month"), value.get("day")
        )
    elif kind == Kind.LOCATION:
        if location_name and location_name.strip():
            location = resolve_location(location_name, user)
        if location is None:
            raise ValidationError(_("Escribe dónde se hizo la foto."))
        location = location.resolved()
        # Kept so the proposal still reads well if a provisional location is removed.
        value = {"location_name": location.name}
        if photo.location_id == location.pk:
            raise ValidationError(_("Esa ubicación ya está aceptada."))
    elif kind == Kind.ADD_FACE:
        face = _create_proposed_face(user, photo, value)
        value = {"x": face.x, "y": face.y, "w": face.w, "h": face.h}
    else:
        raise ValidationError(_("Tipo de propuesta desconocido."))

    duplicate = _find_duplicate(photo, kind, face, person, event_type, value, location)
    if duplicate:
        return cast_vote(user, duplicate, Vote.UP)

    proposal = Proposal.objects.create(
        photo=photo,
        face=face,
        kind=kind,
        value=value,
        person=person,
        event_type=event_type,
        location=location,
        author=user,
    )
    Vote.objects.create(proposal=proposal, user=user, value=Vote.UP)
    audit.log(
        user, "proposal.create", proposal, kind=kind, photo_id=photo.pk, label=proposal.label()
    )
    if photo.status == Photo.Status.PENDING:
        photo.status = Photo.Status.IN_PROGRESS
        photo.save(update_fields=["status"])
    evaluate(proposal)
    proposal.refresh_from_db()
    return proposal


def _create_proposed_face(user, photo, box):
    try:
        x, y, w, h = (int(round(float(box[k]))) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError(_("Recuadro no válido.")) from exc
    x, y = max(0, x), max(0, y)
    w, h = min(w, photo.width - x), min(h, photo.height - y)
    if w < 8 or h < 8:
        raise ValidationError(_("El recuadro es demasiado pequeño."))
    return Face.objects.create(
        photo=photo,
        x=x,
        y=y,
        w=w,
        h=h,
        source=Face.Source.MANUAL,
        status=Face.Status.PROPOSED,
        created_by=user,
    )


# ---------------------------------------------------------------------------
# Voting
# ---------------------------------------------------------------------------


@transaction.atomic
def cast_vote(user, proposal, value):
    if value not in (Vote.UP, Vote.DOWN):
        raise ValidationError(_("Voto no válido."))
    proposal = Proposal.objects.select_for_update().get(pk=proposal.pk)
    if not proposal.is_open:
        raise ValidationError(_("La propuesta ya está cerrada."))
    vote, _created = Vote.objects.update_or_create(
        proposal=proposal, user=user, defaults={"value": value}
    )
    audit.log(user, "vote.cast", proposal, value=value)
    evaluate(proposal)
    proposal.refresh_from_db()
    if not proposal.is_open:  # this vote decided it
        from gamification.services import scoring

        scoring.vote_decided(vote)
    return proposal


def tally(proposal):
    ups = proposal.votes.filter(value=Vote.UP).count()
    downs = proposal.votes.filter(value=Vote.DOWN).count()
    return ups, downs


def outcome(ups, downs, min_approvals, ratio):
    """Pure decision rule: 'accepted', 'rejected' or None (still open)."""
    total = ups + downs
    if total and ups >= min_approvals and ups / total >= ratio:
        return Status.ACCEPTED
    if downs >= min_approvals and downs > ups:
        return Status.REJECTED
    return None


def evaluate(proposal):
    if not proposal.is_open:
        return proposal.status
    conf = CatalogSettings.load()
    ups, downs = tally(proposal)
    result = outcome(ups, downs, conf.vote_min_approvals, conf.vote_approval_ratio)
    if result == Status.ACCEPTED:
        _accept(proposal)
    elif result == Status.REJECTED:
        _reject(proposal)
    return proposal.status


@transaction.atomic
def admin_decide(admin, proposal, accept):
    """An administrator accepts or rejects a proposal directly."""
    if not admin.is_catalog_admin:
        raise PermissionDenied
    proposal = Proposal.objects.select_for_update().get(pk=proposal.pk)
    if not proposal.is_open:
        raise ValidationError(_("La propuesta ya está cerrada."))
    proposal.decided_by = admin
    if accept:
        _accept(proposal)
    else:
        _reject(proposal)
    audit.log(admin, "proposal.admin_accept" if accept else "proposal.admin_reject", proposal)
    return proposal


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


def _close(proposal, status):
    from gamification.services import scoring

    previous = proposal.status
    proposal.status = status
    proposal.decided_at = timezone.now()
    proposal.save(update_fields=["status", "decided_at", "decided_by"])
    scoring.proposal_closed(proposal, previous)


def _accept(proposal):
    photo = Photo.objects.select_for_update().get(pk=proposal.photo_id)
    face = proposal.face

    if proposal.kind == Kind.FACE_IDENTITY:
        person = proposal.person.resolved()
        face.person = person
        face.save(update_fields=["person"])
        if person.is_provisional:
            person.is_provisional = False
            person.save(update_fields=["is_provisional"])
    elif proposal.kind == Kind.EVENT:
        photo.event_type = proposal.event_type
        photo.event_description = proposal.value.get("description", "")
        if proposal.event_type.is_provisional:
            proposal.event_type.is_provisional = False
            proposal.event_type.save(update_fields=["is_provisional"])
    elif proposal.kind == Kind.LOCATION:
        photo.location = proposal.location
        if proposal.location.is_provisional:
            proposal.location.is_provisional = False
            proposal.location.save(update_fields=["is_provisional"])
    elif proposal.kind == Kind.DATE:
        value = proposal.value
        photo.date_precision = value["precision"]
        photo.date_value = datetime.date(value["year"], value.get("month", 1), value.get("day", 1))
    elif proposal.kind == Kind.ADD_FACE:
        face.status = Face.Status.ACTIVE
        face.save(update_fields=["status"])
    elif proposal.kind == Kind.REMOVE_FACE:
        face.status = Face.Status.REMOVED
        face.save(update_fields=["status"])

    _close(proposal, Status.ACCEPTED)

    # Competing proposals and the previously accepted value are superseded.
    others = (
        _same_field(proposal)
        .exclude(pk=proposal.pk)
        .filter(status__in=[Status.OPEN, Status.ACCEPTED])
    )
    if proposal.kind == Kind.REMOVE_FACE:
        others = Proposal.objects.filter(face=face, status=Status.OPEN).exclude(pk=proposal.pk)
    for other in others:
        _close(other, Status.SUPERSEDED)
        cleanup_provisional_person(other.person)
        _cleanup_provisional_event_type(other.event_type)
        cleanup_provisional_location(other.location)

    _mark_changed(photo)


def _reject(proposal):
    _close(proposal, Status.REJECTED)
    if proposal.kind == Kind.ADD_FACE and proposal.face:
        proposal.face.status = Face.Status.REJECTED
        proposal.face.save(update_fields=["status"])
        Proposal.objects.filter(face=proposal.face, status=Status.OPEN).update(
            status=Status.SUPERSEDED, decided_at=timezone.now()
        )
    cleanup_provisional_person(proposal.person)
    _cleanup_provisional_event_type(proposal.event_type)
    cleanup_provisional_location(proposal.location)
    photo = Photo.objects.select_for_update().get(pk=proposal.photo_id)
    refresh_photo_status(photo)


def cleanup_provisional_person(person):
    """Delete a provisional person created by a proposal that did not succeed."""
    if person is None or not person.is_provisional:
        return
    person.refresh_from_db()
    in_use = (
        person.faces.exists()
        or person.user_id
        or person.merged_persons.exists()
        or person.proposals.filter(status__in=[Status.OPEN, Status.ACCEPTED]).exists()
        or person.relationships_from.exists()
        or person.relationships_to.exists()
    )
    if not in_use:
        person.delete()


def _cleanup_provisional_event_type(event_type):
    """Delete a user-proposed event type whose proposals did not succeed."""
    if event_type is None or not event_type.is_provisional:
        return
    event_type.refresh_from_db()
    in_use = (
        event_type.photos.exists()
        or event_type.proposals.filter(status__in=[Status.OPEN, Status.ACCEPTED]).exists()
    )
    if not in_use:
        # Closed proposals keep the name in value["event_name"].
        event_type.proposals.update(event_type=None)
        event_type.delete()


def cleanup_provisional_location(location):
    """Delete a user-proposed location whose proposals did not succeed."""
    if location is None or not location.is_provisional:
        return
    location.refresh_from_db()
    in_use = (
        location.photos.exists()
        or location.merged_locations.exists()
        or location.proposals.filter(status__in=[Status.OPEN, Status.ACCEPTED]).exists()
    )
    if not in_use:
        # Closed proposals keep the name in value["location_name"].
        location.proposals.update(location=None)
        location.delete()


def _mark_changed(photo):
    photo.catalog_changed_at = timezone.now()
    refresh_photo_status(
        photo,
        extra_fields=[
            "event_type",
            "event_description",
            "date_precision",
            "date_value",
            "location",
            "catalog_changed_at",
        ],
    )


def is_cataloged(photo):
    if not photo.event_type_id:
        return False
    faces = photo.faces.filter(status=Face.Status.ACTIVE)
    return not faces.filter(person__isnull=True).exists()


def refresh_photo_status(photo, extra_fields=()):
    if is_cataloged(photo):
        status = Photo.Status.CATALOGED
    elif photo.proposals.exists() or photo.faces.filter(person__isnull=False).exists():
        status = Photo.Status.IN_PROGRESS
    else:
        status = Photo.Status.PENDING
    photo.status = status
    photo.save(update_fields=["status", *extra_fields])
    return status


# ---------------------------------------------------------------------------
# Queries used by the UI
# ---------------------------------------------------------------------------


def pending_for_user(user):
    """Open proposals the user has not voted on yet."""
    return (
        Proposal.objects.filter(status=Status.OPEN, photo__discarded=False)
        .exclude(votes__user=user)
        .select_related("photo", "person", "event_type", "location", "face")
    )


def photos_pending_for_user(user):
    return Photo.objects.visible().filter(proposals__in=pending_for_user(user)).distinct()


def event_types():
    return EventType.objects.enabled()


def locations():
    """Locations offered as suggestions (accepted, active and not merged)."""
    return Location.objects.enabled()
