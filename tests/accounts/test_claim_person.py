"""«Soy yo»: a user identifies as a person of the album and picks a profile photo."""

import pytest
from django.urls import reverse

from accounts.models import AuditLog
from accounts.services import users
from catalog.models import Face, Person

pytestmark = pytest.mark.django_db


@pytest.fixture
def ana(photo):
    person = Person.objects.create(display_name="Ana")
    small, big = photo.faces.order_by("w")
    Face.objects.filter(pk__in=[small.pk, big.pk]).update(person=person)
    return person, small, big


def test_claiming_links_the_user(client, alice, ana):
    person, small, big = ana
    client.force_login(alice)
    content = client.get(reverse("person_detail", args=[person.pk])).content.decode()
    assert reverse("person_claim", args=[person.pk]) in content

    client.post(reverse("person_claim", args=[person.pk]))

    person.refresh_from_db()
    assert person.user == alice
    assert AuditLog.objects.filter(user=alice, action="user.claim_person").exists()
    content = client.get(reverse("person_detail", args=[person.pk])).content.decode()
    assert "Eres tú" in content and reverse("person_unclaim", args=[person.pk]) in content


def test_a_person_of_someone_else_cannot_be_claimed(client, alice, bob, ana):
    person, small, big = ana
    users.claim_person(bob, person)
    client.force_login(alice)
    content = client.get(reverse("person_detail", args=[person.pk])).content.decode()
    assert reverse("person_claim", args=[person.pk]) not in content
    client.post(reverse("person_claim", args=[person.pk]))
    person.refresh_from_db()
    assert person.user == bob


def test_claiming_another_person_switches(alice, ana):
    person, small, big = ana
    other = Person.objects.create(display_name="Luis")
    users.claim_person(alice, person)
    users.claim_person(alice, other)
    person.refresh_from_db()
    assert person.user is None and Person.objects.get(user=alice) == other


def test_unclaim(client, alice, ana):
    person, small, big = ana
    users.claim_person(alice, person)
    client.force_login(alice)
    client.post(reverse("person_unclaim", args=[person.pk]))
    person.refresh_from_db()
    assert person.user is None


def test_profile_photo_among_my_faces_only(client, alice, ana, photo):
    person, small, big = ana
    stranger = Face.objects.create(photo=photo, x=1, y=1, w=20, h=20)
    users.claim_person(alice, person)
    client.force_login(alice)

    content = client.get(reverse("profile")).content.decode()
    assert "Tú en el álbum" in content and f'value="{small.pk}"' in content

    client.post(reverse("profile"), {"profile_face": stranger.pk})
    alice.refresh_from_db()
    assert alice.avatar_face is None

    client.post(reverse("profile"), {"profile_face": small.pk})
    alice.refresh_from_db()
    assert alice.avatar_face == small
    # The menu avatar uses it, not the person's main photo (the largest face).
    home = client.get(reverse("home")).content.decode()
    assert reverse("face_image", args=[small.pk]) in home
    assert users.avatar_face_id(alice) == small.pk


def test_profile_photo_falls_back_when_no_longer_valid(alice, ana):
    person, small, big = ana
    users.claim_person(alice, person)
    users.set_profile_face(alice, small.pk)
    Face.objects.filter(pk=small.pk).update(person=None)  # reassigned later
    alice.refresh_from_db()
    assert users.avatar_face_id(alice) == big.pk  # the person's main photo


def test_profile_without_a_person_explains_how(client, alice):
    client.force_login(alice)
    content = client.get(reverse("profile")).content.decode()
    assert "Buscar mi persona" in content
