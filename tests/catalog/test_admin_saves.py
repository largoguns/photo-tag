"""Administrators do not propose: what they save is accepted at once. Panels start folded."""

import re

import pytest
from django.urls import reverse

from catalog.models import Face, Proposal
from catalog.services import voting

pytestmark = pytest.mark.django_db
Kind = Proposal.Kind


def test_each_kind_saved_by_an_admin_is_accepted(photo, admin_user, superadmin, boda):
    face, second = photo.faces.order_by("pk")[:2]
    identity = voting.create_proposal(
        admin_user, photo, Kind.FACE_IDENTITY, face=face, person_name="Ana"
    )
    event = voting.create_proposal(superadmin, photo, Kind.EVENT, event_type=boda)
    date = voting.create_proposal(
        admin_user, photo, Kind.DATE, value={"precision": "year", "year": 1985}
    )
    place = voting.create_proposal(admin_user, photo, Kind.LOCATION, location_name="Casa Greco")
    drawn = voting.create_proposal(
        admin_user, photo, Kind.ADD_FACE, value={"x": 300, "y": 300, "w": 40, "h": 40}
    )
    removed = voting.create_proposal(admin_user, photo, Kind.REMOVE_FACE, face=second)
    for proposal in (identity, event, date, place, drawn, removed):
        assert proposal.status == Proposal.Status.ACCEPTED, proposal.kind
        assert proposal.decided_by is not None
    photo.refresh_from_db()
    face.refresh_from_db()
    assert face.person.display_name == "Ana" and photo.event_type == boda
    assert photo.date_value.year == 1985 and photo.location.name == "Casa Greco"
    assert Face.objects.get(pk=drawn.face_id).status == Face.Status.ACTIVE


def test_an_admin_joining_an_open_proposal_accepts_it(photo, alice, admin_user):
    face = photo.faces.first()
    proposal = voting.create_proposal(
        alice, photo, Kind.FACE_IDENTITY, face=face, person_name="Ana"
    )
    assert proposal.status == Proposal.Status.OPEN
    same = voting.create_proposal(
        admin_user, photo, Kind.FACE_IDENTITY, face=face, person_name="Ana"
    )
    assert same.pk == proposal.pk and same.status == Proposal.Status.ACCEPTED


def test_collaborators_still_propose(photo, alice):
    face = photo.faces.first()
    proposal = voting.create_proposal(
        alice, photo, Kind.FACE_IDENTITY, face=face, person_name="Ana"
    )
    assert proposal.status == Proposal.Status.OPEN


def test_buttons_say_save_for_admins(client, photo, alice, admin_user):
    client.force_login(admin_user)
    content = client.get(reverse("photo_detail", args=[photo.pk])).content.decode()
    assert "Guardar evento" in content and "Proponer evento" not in content
    client.force_login(alice)
    content = client.get(reverse("photo_detail", args=[photo.pk])).content.decode()
    assert "Proponer evento" in content


def test_panels_start_folded_and_the_used_one_stays_open(client, photo, alice):
    client.force_login(alice)
    content = client.get(reverse("photo_detail", args=[photo.pk])).content.decode()
    for panel in ("event-panel", "date-panel", "location-panel"):
        assert re.search(rf'<details class="panel" id="{panel}"\s*>', content), panel
    response = client.post(
        reverse("propose_date", args=[photo.pk]),
        {"precision": "year", "year": "1985", "panel": "date"},
        HTTP_HX_REQUEST="true",
    )
    content = response.content.decode()
    assert '<details class="panel" id="date-panel" open>' in content
    assert re.search(r'<details class="panel" id="event-panel"\s*>', content)
