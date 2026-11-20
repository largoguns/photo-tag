"""Event type moves used by the administrator review (catalog_review): a type that was
really a place becomes a location, and a variant is merged into another type. Photos and
proposals (open, accepted or rejected, with their votes) follow, and the type is deleted.
"""

from django.db import transaction
from django.utils import timezone

from accounts.services import audit
from catalog.models import EventType, Photo, Proposal
from catalog.services import voting


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
