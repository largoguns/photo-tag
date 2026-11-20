"""Review of the event types the family added with the old «Otro…» (spec 5.5, eventos).

Event types are a standard list. The ones typed by the family are reviewed by an
administrator, one by one, and turned into what they really were:

- a place («Casa Greco») → a location of those photos;
- a personal event («Viaje Mérida») → «Otro» with that text as description;
- a variant of a standard type («Cumple») → merged into it;
- a real event type («Carnavales») → kept as standard.

Photos and proposals (open, accepted or rejected) follow, and the type is deleted.
"""

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from accounts.services import audit
from catalog.models import EventType, Photo, Proposal
from catalog.services import voting

OTHER = voting.OTHER_EVENT


def family_added():
    """Event types typed by the family, with how many photos and proposals use them."""
    return (
        EventType.objects.filter(created_by__isnull=False)
        .select_related("created_by")
        .annotate(
            photo_count=Count("photos", filter=Q(photos__discarded=False), distinct=True),
            proposal_count=Count("proposals", distinct=True),
        )
        .order_by("name")
    )


def other_type():
    other, _created = EventType.objects.get_or_create(
        name=OTHER, defaults={"name_en": "Other", "order": 999}
    )
    if not other.active or other.is_provisional:
        other.active, other.is_provisional = True, False
        other.save(update_fields=["active", "is_provisional"])
    return other


def _joined(name, description):
    description = " ".join((description or "").split())
    if not description or description.casefold() == name.casefold():
        return name
    return f"{name} — {description}"[:255]


def _finish(event_type, photo_ids):
    """Photos re-exported and their status refreshed; the reviewed type goes."""
    Photo.objects.filter(pk__in=photo_ids).update(catalog_changed_at=timezone.now())
    for photo in Photo.objects.filter(pk__in=photo_ids):
        voting.refresh_photo_status(photo)
    event_type.delete()


@transaction.atomic
def to_location(admin, event_type):
    """It was a place: its photos get that location; its proposals become location ones."""
    event_type = EventType.objects.select_for_update().get(pk=event_type.pk)
    location = voting.resolve_location(event_type.name, admin)
    if location.is_provisional:
        location.is_provisional = False
        location.save(update_fields=["is_provisional"])
    photo_ids = list(event_type.photos.values_list("pk", flat=True))
    Photo.objects.filter(pk__in=photo_ids, location__isnull=True).update(location=location)
    Photo.objects.filter(pk__in=photo_ids).update(event_type=None, event_description="")
    event_type.proposals.update(
        kind=Proposal.Kind.LOCATION,
        event_type=None,
        location=location,
        value={"location_name": location.name},
    )
    audit.log(
        admin, "event_type.to_location", location, name=event_type.name, photos=len(photo_ids)
    )
    _finish(event_type, photo_ids)
    return location


@transaction.atomic
def to_other(admin, event_type):
    """It was a personal event: «Otro», with its name as the description."""
    event_type = EventType.objects.select_for_update().get(pk=event_type.pk)
    other = other_type()
    photo_ids = []
    for photo in event_type.photos.all():
        photo.event_type = other
        photo.event_description = _joined(event_type.name, photo.event_description)
        photo.save(update_fields=["event_type", "event_description"])
        photo_ids.append(photo.pk)
    for proposal in event_type.proposals.all():
        description = _joined(event_type.name, proposal.value.get("description", ""))
        proposal.event_type = other
        proposal.value = {**proposal.value, "description": description, "event_name": other.name}
        proposal.save(update_fields=["event_type", "value"])
    audit.log(admin, "event_type.to_other", other, name=event_type.name, photos=len(photo_ids))
    _finish(event_type, photo_ids)
    return other


@transaction.atomic
def merge(admin, event_type, target):
    """It was a variant of a standard type: photos and proposals move to `target`."""
    if target.pk == event_type.pk:
        raise ValueError("Cannot merge an event type into itself")
    event_type = EventType.objects.select_for_update().get(pk=event_type.pk)
    photo_ids = list(event_type.photos.values_list("pk", flat=True))
    Photo.objects.filter(pk__in=photo_ids).update(event_type=target)
    for proposal in event_type.proposals.all():
        proposal.event_type = target
        proposal.value = {**proposal.value, "event_name": target.name}
        proposal.save(update_fields=["event_type", "value"])
    audit.log(admin, "event_type.merge", target, name=event_type.name, photos=len(photo_ids))
    _finish(event_type, photo_ids)
    return target


def keep(admin, event_type):
    """It is a real event type: it joins the standard list."""
    event_type.created_by = None
    event_type.is_provisional = False
    event_type.active = True
    event_type.save(update_fields=["created_by", "is_provisional", "active"])
    audit.log(admin, "event_type.keep", event_type, name=event_type.name)
    return event_type
