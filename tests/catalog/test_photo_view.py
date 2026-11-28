"""Photo page: view mode for complete photos and folded consolidated faces (spec 5.5)."""

import datetime

import pytest
from django.urls import reverse
from django.utils import translation

from catalog.models import EventType, Face, Location, Person, Photo, Proposal
from catalog.services import questions, voting
from catalog.services.names import name_key

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def spanish():
    with translation.override("es"):
        yield


@pytest.fixture
def complete(photo):
    """Every face with an accepted person, the event, the date and the location."""
    for index, face in enumerate(photo.faces.order_by("x")):
        person = Person.objects.create(display_name=f"Persona {index}")
        Face.objects.filter(pk=face.pk).update(person=person)
    home = Location.objects.create(name="Casa", name_key=name_key("Casa"), locality="Sevilla")
    Photo.objects.filter(pk=photo.pk).update(
        status=Photo.Status.CATALOGED,
        event_type=EventType.objects.get(name="Boda"),
        date_value=datetime.date(1992, 6, 14),
        date_precision=Photo.DatePrecision.EXACT,
        location=home,
    )
    photo.refresh_from_db()
    return photo


def page(client, photo, **params):
    return client.get(reverse("photo_detail", args=[photo.pk]), params).content.decode()


def is_view_mode(content):
    return 'class="photo-layout view-mode"' in content


def test_a_complete_photo_opens_in_view_mode(client, complete, alice):
    client.force_login(alice)
    content = page(client, complete)
    assert is_view_mode(content)
    assert 'class="photo-facts"' in content and "Persona 0" in content
    assert "Boda" in content and "14/06/1992" in content and "Casa · Sevilla" in content
    assert "Compartir" in content and "?edit=1" in content
    assert 'data-action="draw-face"' not in content  # no editing tools
    assert "rotate_photo" not in content and 'id="event-panel"' not in content
    assert "admin_photo_delete" not in content and "Borrar foto" not in content  # admins only
    assert 'data-highlight-face="' in content  # names mark their face


def test_administrators_can_delete_from_view_mode_with_confirmation(client, complete, admin_user):
    client.force_login(admin_user)
    content = page(client, complete)
    actions = content.split('class="photo-toolbar photo-actions"')[1].split("</section>")[0]
    assert reverse("admin_photo_delete", args=[complete.pk]) in actions
    assert "data-confirm=" in actions


@pytest.mark.parametrize("missing", ["date", "location", "status", "proposal", "question"])
def test_anything_missing_opens_the_editor(client, complete, alice, bob, missing):
    if missing == "date":
        Photo.objects.filter(pk=complete.pk).update(date_value=None, date_precision="")
    elif missing == "location":
        Photo.objects.filter(pk=complete.pk).update(location=None)
    elif missing == "status":
        Photo.objects.filter(pk=complete.pk).update(status=Photo.Status.IN_PROGRESS)
    elif missing == "proposal":
        voting.create_proposal(
            bob, complete, Proposal.Kind.DATE, value={"precision": "year", "year": "1993"}
        )
    else:
        questions.ask(bob, complete, "general", "¿Seguro que es de 1992?")
    client.force_login(alice)
    content = page(client, complete)
    assert not is_view_mode(content) and 'data-action="draw-face"' in content


def test_edit_opens_the_editor_and_offers_to_go_back(client, complete, alice):
    client.force_login(alice)
    content = page(client, complete, edit="1")
    assert not is_view_mode(content) and 'data-action="draw-face"' in content
    assert "Ver foto" in content
    # Actions answer with the editor, so the mode never changes while editing.
    partial = client.get(
        reverse("photo_detail", args=[complete.pk]), HTTP_HX_REQUEST="true"
    ).content.decode()
    assert not is_view_mode(partial)


def test_consolidated_faces_start_folded_with_a_green_number(client, complete, alice, bob):
    first, second = complete.faces.order_by("x")
    voting.create_proposal(
        bob, complete, Proposal.Kind.FACE_IDENTITY, face=second, person_name="Otra persona"
    )
    client.force_login(alice)
    content = page(client, complete, edit="1", face=first.pk)
    panel = content.split(f'id="face-panel-{first.pk}"')[0].rsplit("<details", 1)[1]
    assert "consolidated" in panel
    opening = content.split(f'id="face-panel-{first.pk}"')[1].split(">")[0]
    assert "open" not in opening  # folded even though it is the selected face
    content = page(client, complete, edit="1", face=second.pk)
    opening = content.split(f'id="face-panel-{second.pk}"')[1].split(">")[0]
    assert "open" in opening  # an open proposal: not consolidated
