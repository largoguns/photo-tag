"""Administrator review of new event types and locations (spec section 5.5).

Choosing «Otro…» and typing an event, or typing a new place, creates a provisional entry
(`is_provisional`). Its proposals are not voted: an administrator accepts the entry (as
typed or with a better text, merging it with an existing one when they match), rejects it,
or (for events) says it is a place. Accepting or rejecting decides its proposals.
"""

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from accounts.services import audit
from catalog.models import EventType, Location, Photo, Proposal
from catalog.services import event_types, locations, voting
from catalog.services.names import name_key, normalize_label

OPEN = Proposal.Status.OPEN


def _counts(queryset):
    return queryset.select_related("created_by").annotate(
        photo_count=Count("photos", filter=Q(photos__discarded=False), distinct=True),
        open_count=Count("proposals", filter=Q(proposals__status=OPEN), distinct=True),
    )


def pending_event_types():
    return _counts(EventType.objects.filter(is_provisional=True)).order_by("name")


def pending_locations():
    return _counts(Location.objects.filter(is_provisional=True, merged_into__isnull=True)).order_by(
        "name"
    )


def first_photo_id(entry):
    """A photo where the entry is used, to see it in context before deciding."""
    proposal = entry.proposals.filter(status=OPEN).order_by("created_at").first()
    if proposal:
        return proposal.photo_id
    return entry.photos.values_list("pk", flat=True).first()


def _decide_open(admin, proposal_ids, accept):
    for proposal in Proposal.objects.filter(pk__in=proposal_ids, status=OPEN):
        voting.admin_decide(admin, proposal, accept=accept)


def _open_ids(entry):
    return list(entry.proposals.filter(status=OPEN).values_list("pk", flat=True))


def _find_existing(entry, text):
    key = name_key(text)
    if isinstance(entry, EventType):
        for other in EventType.objects.exclude(pk=entry.pk).filter(is_provisional=False):
            if key in (name_key(other.name), name_key(other.name_en)):
                return other
        return None
    other = Location.objects.filter(name_key=key).exclude(pk=entry.pk).first()
    return other.resolved() if other else None


@transaction.atomic
def accept(admin, entry, text=None, locality=None):
    """Accept as typed or with `text`; matching an existing entry merges into it.
    Locations also take the reviewed `locality` (None leaves it as proposed)."""
    ids = _open_ids(entry)
    text = " ".join(str(text or "").split())
    if text and text != entry.name:
        existing = _find_existing(entry, text)
        if existing is not None:
            if isinstance(entry, EventType):
                event_types.merge(admin, entry, existing)
            else:
                locations.merge_locations(admin, entry, existing)
            entry = existing
        elif isinstance(entry, EventType):
            old = entry.name
            entry.name = normalize_label(text)[:100]
            entry.save(update_fields=["name"])
            Photo.objects.filter(event_type=entry).update(catalog_changed_at=timezone.now())
            audit.log(admin, "event_type.update", entry, name=entry.name, old=old)
        else:
            locations.rename_location(admin, entry, text)
    if entry.is_provisional:
        entry.is_provisional = False
        entry.active = True
        entry.save(update_fields=["is_provisional", "active"])
    if isinstance(entry, Location) and locality is not None:
        cleaned = locations.clean_locality(locality)
        if cleaned != entry.locality:
            entry.locality = cleaned
            entry.save(update_fields=["locality"])
    _decide_open(admin, ids, accept=True)
    action = "event_type.accept" if isinstance(entry, EventType) else "location.accept"
    audit.log(admin, action, entry, name=entry.name, proposals=len(ids))
    return entry


@transaction.atomic
def reject(admin, entry):
    """Not valid: its proposals are rejected and photos that had it lose it."""
    is_event = isinstance(entry, EventType)
    name = entry.name
    ids = _open_ids(entry)
    _decide_open(admin, ids, accept=False)
    photo_ids = list(entry.photos.values_list("pk", flat=True))
    if is_event:
        Photo.objects.filter(pk__in=photo_ids).update(event_type=None, event_description="")
        Proposal.objects.filter(event_type=entry).update(event_type=None)
        model = EventType
    else:
        Photo.objects.filter(pk__in=photo_ids).update(location=None)
        Proposal.objects.filter(location=entry).update(location=None)
        model = Location
    Photo.objects.filter(pk__in=photo_ids).update(catalog_changed_at=timezone.now())
    for photo in Photo.objects.filter(pk__in=photo_ids):
        voting.refresh_photo_status(photo)
    model.objects.filter(pk=entry.pk).delete()
    action = "event_type.reject" if is_event else "location.reject"
    audit.log(admin, action, None, name=name, proposals=len(ids), photos=len(photo_ids))


@transaction.atomic
def to_location(admin, event_type):
    """It was typed as an event but it is a place: it becomes an accepted location."""
    ids = _open_ids(event_type)
    location = event_types.to_location(admin, event_type)
    _decide_open(admin, ids, accept=True)
    return location
