"""Main photo of a person: chosen face, fallbacks and the selection sheet."""

import pytest
from django.urls import reverse

from catalog.models import Face, Person
from catalog.presenters import person_avatars
from catalog.services import avatars

pytestmark = pytest.mark.django_db


@pytest.fixture
def maria(photo):
    person = Person.objects.create(display_name="María")
    small, big = photo.faces.order_by("w")
    Face.objects.filter(pk__in=[small.pk, big.pk]).update(person=person)
    return person, small, big


def test_largest_face_is_the_default(maria):
    person, small, big = maria
    assert person_avatars([person.pk]) == {person.pk: big.pk}


def test_chosen_face_wins(maria, alice):
    person, small, big = maria
    avatars.set_avatar(alice, person, small.pk)
    assert person_avatars([person.pk]) == {person.pk: small.pk}
    assert avatars.avatar_face_id(person) == small.pk


def test_chosen_face_no_longer_valid_falls_back(maria, alice):
    person, small, big = maria
    avatars.set_avatar(alice, person, small.pk)
    Face.objects.filter(pk=small.pk).update(person=None)  # reassigned by a later vote
    assert person_avatars([person.pk]) == {person.pk: big.pk}


def test_face_of_someone_else_is_refused(maria, alice, photo):
    person, small, big = maria
    other = Face.objects.create(photo=photo, x=10, y=10, w=50, h=50, score=0.9)
    with pytest.raises(ValueError):
        avatars.set_avatar(alice, person, other.pk)


def test_person_detail_offers_every_face(client, alice, maria):
    person, small, big = maria
    client.force_login(alice)
    content = client.get(reverse("person_detail", args=[person.pk])).content.decode()
    assert 'id="sheet-avatar"' in content and "Cambiar foto principal" in content
    assert f'value="{small.pk}"' in content and f'value="{big.pk}"' in content


def test_choosing_from_the_sheet(client, alice, maria):
    person, small, big = maria
    client.force_login(alice)
    response = client.post(reverse("person_avatar", args=[person.pk]), {"face": small.pk})
    assert response.status_code == 302
    person.refresh_from_db()
    assert person.avatar_face == small
    persons = client.get(reverse("persons")).content.decode()
    assert reverse("face_image", args=[small.pk]) in persons


def test_choosing_a_bad_face_changes_nothing(client, alice, maria):
    person, small, big = maria
    client.force_login(alice)
    client.post(reverse("person_avatar", args=[person.pk]), {"face": "nope"})
    person.refresh_from_db()
    assert person.avatar_face is None


def test_nav_avatar_uses_the_chosen_face(client, alice, maria):
    person, small, big = maria
    person.user = alice
    person.save()
    avatars.set_avatar(alice, person, small.pk)
    client.force_login(alice)
    content = client.get(reverse("home")).content.decode()
    assert reverse("face_image", args=[small.pk]) in content


# --- Renaming ---------------------------------------------------------------------------


def test_admin_renames_a_person_and_photos_are_re_exported(client, admin_user, maria):
    person, small, big = maria
    client.force_login(admin_user)
    content = client.get(reverse("person_detail", args=[person.pk])).content.decode()
    assert reverse("person_rename", args=[person.pk]) in content

    client.post(reverse("person_rename", args=[person.pk]), {"display_name": "maría de la luz"})

    person.refresh_from_db()
    assert person.display_name == "María de la Luz"  # normalized like any new name
    assert small.photo.__class__.objects.get(pk=small.photo_id).catalog_changed_at
    from accounts.models import AuditLog

    entry = AuditLog.objects.get(action="person.rename")
    assert entry.details == {"old": "María", "new": "María de la Luz"}


def test_rename_allows_a_repeated_name_with_a_warning(client, admin_user, maria):
    # A grandmother and her granddaughter can both be called Pedro… or Ana.
    person, small, big = maria
    Person.objects.create(display_name="Pedro")
    client.force_login(admin_user)
    response = client.post(
        reverse("person_rename", args=[person.pk]), {"display_name": "PEDRO"}, follow=True
    )
    content = response.content.decode()
    assert "message-warning" in content and "Fusionar personas" in content
    person.refresh_from_db()
    assert person.display_name == "Pedro"

    client.post(reverse("person_rename", args=[person.pk]), {"display_name": "Pedro Luis"})
    response = client.get(reverse("person_detail", args=[person.pk]))
    assert "message-warning" not in response.content.decode()  # no namesake, no warning


def test_collaborators_cannot_rename(client, alice, maria):
    person, small, big = maria
    client.force_login(alice)
    assert (
        client.post(reverse("person_rename", args=[person.pk]), {"display_name": "X"}).status_code
        == 403
    )
    content = client.get(reverse("person_detail", args=[person.pk])).content.decode()
    assert reverse("person_rename", args=[person.pk]) not in content
