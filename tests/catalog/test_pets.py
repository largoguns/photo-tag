"""Pets in the album (spec section 5.16)."""

import random

import numpy as np
import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import translation

from accounts.models import AuditLog
from catalog.models import Face, Person, Photo, Relationship
from catalog.services import person_details, relationships, similarity
from exporter.services import xmp

pytestmark = pytest.mark.django_db
K = Relationship.Kind


@pytest.fixture(autouse=True)
def spanish():
    with translation.override("es"):
        yield


@pytest.fixture
def curro():
    return Person.objects.create(display_name="Curro")


@pytest.fixture
def adrian():
    return Person.objects.create(display_name="Adrián")


def embed(face, vector):
    vector = np.asarray(vector, dtype=np.float32)
    vector = vector / np.linalg.norm(vector)
    padded = np.zeros(similarity.EMBEDDING_DIM, dtype=np.float32)
    padded[: len(vector)] = vector
    Face.objects.filter(pk=face.pk).update(embedding=padded.tobytes())


# --- Marking --------------------------------------------------------------------------------


def test_mark_and_unmark_a_pet(curro, alice, photo):
    face = photo.faces.first()
    Face.objects.filter(pk=face.pk).update(person=curro)
    person_details.set_pet(alice, curro, True)
    curro.refresh_from_db()
    photo.refresh_from_db()
    assert curro.is_pet and photo.catalog_changed_at  # its photos are exported again
    assert AuditLog.objects.filter(action="person.pet").exists()
    person_details.set_pet(alice, curro, False)
    curro.refresh_from_db()
    assert not curro.is_pet


def test_a_person_with_family_or_a_user_cannot_become_a_pet(curro, adrian, alice):
    relationships.add(alice, adrian, "sibling", other=curro)
    with pytest.raises(ValidationError, match="quítalas antes"):
        person_details.set_pet(alice, curro, True)
    Relationship.objects.all().delete()
    curro.user = alice
    curro.save()
    with pytest.raises(ValidationError, match="No soy yo"):
        person_details.set_pet(alice, curro, True)


def test_a_pet_with_owners_cannot_go_back_to_person(curro, adrian, alice):
    person_details.set_pet(alice, curro, True)
    relationships.add(alice, adrian, "pet", other=curro)
    with pytest.raises(ValidationError, match="Mascota de"):
        person_details.set_pet(alice, curro, False)


# --- Relationships --------------------------------------------------------------------------


def test_pet_of_is_stored_owner_to_pet(curro, adrian, alice):
    person_details.set_pet(alice, curro, True)
    # From either side: «Curro es mascota de Adrián» and «Adrián es dueño de Curro».
    relationships.add(alice, adrian, "pet", other=curro)
    relationship = Relationship.objects.get()
    assert (relationship.person_from, relationship.person_to, relationship.kind) == (
        adrian,
        curro,
        K.OWNER,
    )
    relationship.delete()
    relationships.add(alice, curro, "owner", other=adrian)
    assert Relationship.objects.get().person_from == adrian


def test_pets_only_have_pet_relationships(curro, adrian, alice):
    with pytest.raises(ValidationError, match="Solo una mascota"):
        relationships.add(alice, adrian, "pet", other=curro)  # Curro is not a pet yet
    person_details.set_pet(alice, curro, True)
    with pytest.raises(ValidationError, match="solo puede tener"):
        relationships.add(alice, adrian, "sibling", other=curro)
    with pytest.raises(ValidationError, match="solo puede tener"):
        relationships.add(alice, adrian, "friend", other=curro)
    other_pet = Person.objects.create(display_name="Lula", is_pet=True)
    with pytest.raises(ValidationError, match="no puede tener mascotas"):
        relationships.add(alice, curro, "pet", other=other_pet)


def test_the_relationship_sheet_offers_the_right_roles(client, curro, adrian, alice):
    person_details.set_pet(alice, curro, True)
    client.force_login(alice)
    pet_page = client.get(reverse("person_detail", args=[curro.pk])).content.decode()
    pet_roles = pet_page.split('id="relation-role"')[1].split("</select>")[0]
    assert 'value="owner"' in pet_roles and 'value="sibling"' not in pet_roles
    person_page = client.get(reverse("person_detail", args=[adrian.pk])).content.decode()
    person_roles = person_page.split('id="relation-role"')[1].split("</select>")[0]
    assert 'value="pet"' in person_roles and 'value="owner"' not in person_roles


# --- Face suggestions -----------------------------------------------------------------------


def test_pets_are_never_suggested_and_get_no_candidates(photo, curro, adrian, alice):
    pet_face, human_face = photo.faces.all()[:2]
    other = Photo.objects.create(scan=photo.scan, file_name="o.jpg", index=1, width=10, height=10)
    # An unnamed face in another photo (people already in a photo are never suggested).
    unknown = Face.objects.create(photo=other, x=1, y=1, w=5, h=5)
    Face.objects.filter(pk=human_face.pk).update(person=adrian)
    Face.objects.filter(pk=pet_face.pk).update(person=curro)
    embed(pet_face, [1, 0])
    embed(unknown, [1, 0.01])  # looks exactly like the pet's face
    embed(human_face, [0, 1])
    assert similarity.suggestions_for_face(Face.objects.get(pk=unknown.pk))  # before: suggested

    person_details.set_pet(alice, curro, True)

    names = [s.person for s in similarity.suggestions_for_face(Face.objects.get(pk=unknown.pk))]
    assert curro not in names
    assert similarity.candidate_faces_for_person(curro) == []
    assert similarity.candidate_counts([curro.pk]) == {}


def test_pet_page_has_no_claim_or_candidates_and_shows_the_badge(client, curro, alice):
    person_details.set_pet(alice, curro, True)
    client.force_login(alice)
    content = client.get(reverse("person_detail", args=[curro.pk])).content.decode()
    assert "🐾 Mascota" in content and "Es una persona" in content
    assert reverse("person_claim", args=[curro.pk]) not in content
    response = client.post(reverse("person_claim", args=[curro.pk]), follow=True)
    assert "Es una mascota" in response.content.decode()
    curro.refresh_from_db()
    assert curro.user is None


def test_mark_from_the_menu(client, curro, alice):
    client.force_login(alice)
    content = client.get(reverse("person_detail", args=[curro.pk])).content.decode()
    assert "Es una mascota" in content
    client.post(reverse("set_pet", args=[curro.pk]), {"is_pet": "1"})
    curro.refresh_from_db()
    assert curro.is_pet


# --- Lists ----------------------------------------------------------------------------------


def test_persons_list_has_a_pets_tab(client, curro, adrian, alice):
    client.force_login(alice)
    assert "Mascotas" not in client.get(reverse("persons")).content.decode()  # none yet
    person_details.set_pet(alice, curro, True)
    people = client.get(reverse("persons"))
    assert "🐾 Mascotas" in people.content.decode()
    assert [p.display_name for p in people.context["page"]] == ["Adrián"]
    pets = client.get(reverse("persons"), {"type": "pets"}).context["page"]
    assert [p.display_name for p in pets] == ["Curro"]


def test_people_json_marks_pets(client, photo, curro, alice):
    person_details.set_pet(alice, curro, True)
    client.force_login(alice)
    entries = client.get(reverse("photo_detail", args=[photo.pk])).context["people_json"]
    assert next(e for e in entries if e["name"] == "Curro")["pet"] is True


# --- Export and quiz ------------------------------------------------------------------------


def test_pet_regions_are_exported_as_pet(photo, curro, adrian, alice):
    pet_face, human_face = photo.faces.all()[:2]
    Face.objects.filter(pk=pet_face.pk).update(person=curro)
    Face.objects.filter(pk=human_face.pk).update(person=adrian)
    person_details.set_pet(alice, curro, True)
    types = {r["name"]: r["type"] for r in xmp.face_regions(photo)}
    assert types == {"Curro": "Pet", "Adrián": "Face"}
    assert "Name=Curro,Type=Pet" in xmp.region_info_value(photo, xmp.face_regions(photo))


def test_quiz_asks_about_pets_among_pets(alice):
    from gamification.services import quiz

    scan = Photo.objects.model.scan.field.related_model.objects.create(
        sha256="p" * 64, file_name="pets.jpg"
    )
    names = [f"Persona {i}" for i in range(10)] + ["Curro", "Lula", "Toby", "Nala"]
    for index, name in enumerate(names):
        photo = Photo.objects.create(
            scan=scan, file_name=f"p{index}.jpg", index=index, width=10, height=10
        )
        person = Person.objects.create(display_name=name, is_pet=index >= 10)
        Face.objects.create(photo=photo, x=1, y=1, w=5, h=5, person=person)
    pets = {"Curro", "Lula", "Toby", "Nala"}
    for seed in range(8):
        attempt = quiz.start(alice, quiz.Kind.FACES, rng=random.Random(seed))
        for question in attempt.questions:
            labels = {option["label"] for option in question["options"]}
            assert labels <= pets or not labels & pets  # never mixed
