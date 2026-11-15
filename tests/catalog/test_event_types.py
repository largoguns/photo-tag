import pytest
from django.urls import reverse

from catalog.models import EventType, Proposal, Vote
from catalog.services import voting
from catalog.services.names import normalize_label

pytestmark = pytest.mark.django_db


def propose(user, photo, name, description=""):
    return voting.create_proposal(
        user, photo, Proposal.Kind.EVENT, event_name=name, value={"description": description}
    )


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


def test_other_event_creates_a_provisional_type(photo, alice):
    proposal = propose(alice, photo, "graduación")

    event_type = proposal.event_type
    assert event_type.name == "Graduación"
    assert event_type.is_provisional and event_type.created_by == alice
    assert proposal.label() == "Graduación"


def test_same_name_reuses_the_type_ignoring_case_and_accents(photo, alice, bob):
    first = propose(alice, photo, "graduación")
    second = propose(bob, photo, "GRADUACION")
    assert second == first  # identical proposal → bob's vote is added
    assert EventType.objects.filter(name__iexact="graduación").count() == 1


def test_existing_types_are_matched_in_both_languages(photo, alice):
    assert propose(alice, photo, "wedding").event_type.name == "Boda"
    assert propose(alice, photo, "boda").event_type.name == "Boda"


def test_accepting_makes_the_type_permanent(photo, alice, bob):
    proposal = propose(alice, photo, "Graduación")
    voting.cast_vote(bob, proposal, Vote.UP)

    photo.refresh_from_db()
    assert photo.event_type.name == "Graduación"
    assert not photo.event_type.is_provisional
    assert photo.event_type in voting.event_types()


def test_rejecting_removes_an_unused_provisional_type(photo, alice, bob, carol):
    proposal = propose(alice, photo, "Broma")
    voting.cast_vote(bob, proposal, Vote.DOWN)
    voting.cast_vote(carol, proposal, Vote.DOWN)

    assert not EventType.objects.filter(name="Broma").exists()
    proposal.refresh_from_db()
    assert proposal.event_type is None
    assert proposal.label() == "Broma"  # the closed proposal still reads well


def test_superseded_provisional_type_is_removed(photo, alice, bob, boda):
    voting.create_proposal(alice, photo, Proposal.Kind.EVENT, event_type=boda)
    other = propose(bob, photo, "Bautizo de Ana")
    accepted = Proposal.objects.get(event_type=boda)
    voting.cast_vote(bob, accepted, Vote.UP)

    other.refresh_from_db()
    assert other.status == Proposal.Status.SUPERSEDED
    assert not EventType.objects.filter(name="Bautizo de Ana").exists()


def test_other_type_is_hidden_and_provisional_types_not_listed(client, photo, alice):
    EventType.objects.create(name="Pendiente", is_provisional=True)
    client.force_login(alice)
    content = client.get(reverse("photo_detail", args=[photo.pk])).content.decode()
    options = content.split('id="event-type"')[1].split("</select>")[0]
    assert ">Boda<" in options
    assert ">Otro<" not in options and "Pendiente" not in options
    assert 'value="new"' in options and 'name="event_name"' in content


def test_propose_new_event_from_the_form(client, photo, alice):
    client.force_login(alice)
    client.post(
        reverse("propose_event", args=[photo.pk]),
        {"event_type": "new", "event_name": "comunión de pablo", "description": ""},
    )
    proposal = Proposal.objects.get()
    assert proposal.event_type.name == "Comunión de pablo"


def test_admin_list_marks_user_proposed_types(client, admin_user, alice):
    EventType.objects.create(name="Graduación", is_provisional=True, created_by=alice)
    client.force_login(admin_user)
    content = client.get(reverse("admin_event_types")).content.decode()
    assert "Pendiente de votación" in content and "Propuesto por Alicia" in content
