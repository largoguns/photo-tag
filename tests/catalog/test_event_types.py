"""Event types are a standard list; «Otro» + description; review of family-added types."""

import pytest
from django.urls import reverse

from accounts.models import AuditLog
from catalog.models import EventType, Location, Photo, Proposal, Vote
from catalog.services import event_types, voting
from catalog.services.names import normalize_label

pytestmark = pytest.mark.django_db


@pytest.fixture
def other(db):
    return event_types.other_type()


@pytest.fixture
def junk(alice):
    """A type typed by the family with the old «Otro…»."""
    return EventType.objects.create(name="Viaje Mérida", created_by=alice)


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("graduación", "Graduación"),
        ("fin de CURSO", "Fin de CURSO"),
        ("FIESTA DEL PUEBLO", "Fiesta del pueblo"),
    ],
)
def test_normalize_label(typed, expected):
    assert normalize_label(typed) == expected


# --- Proposing --------------------------------------------------------------------------


def test_other_is_standard_and_family_types_are_not_offered(other, junk, boda):
    offered = list(voting.event_types())
    assert other in offered and boda in offered and junk not in offered


def test_a_typed_name_no_longer_creates_types(client, photo, alice, boda):
    client.force_login(alice)
    count = EventType.objects.count()
    client.post(
        reverse("propose_event", args=[photo.pk]),
        {"event_type": "new", "event_name": "Casa del pueblo"},
        HTTP_HX_REQUEST="true",
    )
    assert EventType.objects.count() == count
    assert not Proposal.objects.exists()


def test_other_needs_a_description(photo, alice, other):
    with pytest.raises(Exception, match="descripción"):
        voting.create_proposal(alice, photo, Proposal.Kind.EVENT, event_type=other)
    proposal = voting.create_proposal(
        alice, photo, Proposal.Kind.EVENT, event_type=other, value={"description": "Viaje"}
    )
    assert proposal.event_type == other and proposal.value["description"] == "Viaje"


def test_event_form_offers_the_standard_list_with_other(client, photo, alice, other, junk):
    client.force_login(alice)
    content = client.get(reverse("photo_detail", args=[photo.pk])).content.decode()
    assert "data-other" in content and "Viaje Mérida" not in content
    assert 'name="event_name"' not in content and "Otro…" not in content


# --- Review of family-added types -------------------------------------------------------


def family_photo(photo, junk, alice, bob):
    """The junk type accepted on a photo, plus an open proposal elsewhere."""
    Photo.objects.filter(pk=photo.pk).update(event_type=junk, event_description="con Ana")
    other_photo = Photo.objects.create(
        scan=photo.scan, file_name="x.jpg", index=9, width=10, height=10
    )
    open_proposal = Proposal.objects.create(
        photo=other_photo, kind=Proposal.Kind.EVENT, event_type=junk, author=alice, value={}
    )
    Vote.objects.create(proposal=open_proposal, user=alice, value=Vote.UP)
    return other_photo, open_proposal


def test_to_location(photo, junk, alice, bob, admin_user):
    other_photo, proposal = family_photo(photo, junk, alice, bob)
    location = event_types.to_location(admin_user, junk)

    photo.refresh_from_db()
    proposal.refresh_from_db()
    assert photo.location == location and photo.event_type is None
    assert location.name == "Viaje Mérida" and not location.is_provisional
    assert proposal.kind == Proposal.Kind.LOCATION and proposal.location == location
    assert proposal.votes.count() == 1  # votes kept
    assert not EventType.objects.filter(pk=junk.pk).exists()
    assert AuditLog.objects.filter(action="event_type.to_location").exists()


def test_to_location_keeps_an_existing_location(photo, junk, alice, bob, admin_user):
    family_photo(photo, junk, alice, bob)
    home = Location.objects.create(name="Casa", name_key="casa")
    Photo.objects.filter(pk=photo.pk).update(location=home)
    event_types.to_location(admin_user, junk)
    photo.refresh_from_db()
    assert photo.location == home


def test_to_other_keeps_the_text_as_description(photo, junk, alice, bob, admin_user, other):
    other_photo, proposal = family_photo(photo, junk, alice, bob)
    event_types.to_other(admin_user, junk)

    photo.refresh_from_db()
    proposal.refresh_from_db()
    assert photo.event_type == other and photo.event_description == "Viaje Mérida — con Ana"
    assert proposal.event_type == other and proposal.value["description"] == "Viaje Mérida"
    assert not EventType.objects.filter(pk=junk.pk).exists()


def test_merge_into_a_standard_type(photo, junk, alice, bob, admin_user, boda):
    other_photo, proposal = family_photo(photo, junk, alice, bob)
    event_types.merge(admin_user, junk, boda)
    photo.refresh_from_db()
    proposal.refresh_from_db()
    assert photo.event_type == boda and proposal.event_type == boda
    assert not EventType.objects.filter(pk=junk.pk).exists()


def test_keep_makes_it_standard(junk, admin_user):
    event_types.keep(admin_user, junk)
    junk.refresh_from_db()
    assert junk.created_by is None and junk in voting.event_types()


def test_review_screen_and_permissions(client, junk, alice, admin_user, boda, photo):
    client.force_login(alice)
    url = reverse("admin_event_type_review", args=[junk.pk])
    assert client.post(url, {"action": "other"}).status_code == 403

    client.force_login(admin_user)
    content = client.get(reverse("admin_event_types")).content.decode()
    assert "Añadido por la familia (1)" in content and "Viaje Mérida" in content

    client.post(url, {"action": "merge", "target": boda.pk})
    assert not EventType.objects.filter(pk=junk.pk).exists()
    assert "Añadido por la familia" not in client.get(reverse("admin_event_types")).content.decode()
