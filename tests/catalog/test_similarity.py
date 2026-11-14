import numpy as np
import pytest
from django.urls import reverse

from catalog.models import Face, Person, Photo, Proposal, Scan
from catalog.services import similarity, voting

pytestmark = pytest.mark.django_db


def unit(*components):
    """A normalized 128-d vector from its first components."""
    vector = np.zeros(128, dtype=np.float32)
    vector[: len(components)] = components
    return (vector / np.linalg.norm(vector)).tobytes()


def make_photo(name):
    scan = Scan.objects.create(sha256=name.ljust(64, "0"), file_name=f"{name}.jpg")
    return Photo.objects.create(scan=scan, file_name=f"{name}.jpg", index=0, width=800, height=600)


def make_face(photo, embedding, person=None, **extra):
    return Face.objects.create(
        photo=photo, x=10, y=10, w=50, h=50, embedding=embedding, person=person, **extra
    )


@pytest.fixture
def people(db):
    carmen = Person.objects.create(display_name="Abuela Carmen")
    paco = Person.objects.create(display_name="Tío Paco")
    make_face(make_photo("ref1"), unit(1, 0), carmen)
    make_face(make_photo("ref2"), unit(0.9, 0.1), carmen)
    make_face(make_photo("ref3"), unit(0, 1), paco)
    return carmen, paco


def test_suggests_most_similar_person_first(people):
    carmen, paco = people
    face = make_face(make_photo("q"), unit(1, 0.3))

    suggestions = similarity.suggestions_for_face(face)

    assert [s.person for s in suggestions] == [carmen]
    assert suggestions[0].strong
    assert suggestions[0].label == "muy probable"


def test_scores_use_best_face_per_person(people):
    carmen, paco = people
    face = make_face(make_photo("q"), unit(1, 1))

    suggestions = similarity.suggestions_for_face(face)

    assert {s.person for s in suggestions} == {carmen, paco}
    assert len(suggestions) == 2  # one entry per person, not per face


def test_below_threshold_gives_no_suggestion(people):
    face = make_face(make_photo("q"), unit(0, 0, 1))
    assert similarity.suggestions_for_face(face) == []


def test_identified_or_unembedded_faces_get_no_suggestion(people):
    carmen, _ = people
    assert similarity.suggestions_for_face(make_face(make_photo("a"), None)) == []
    assert similarity.suggestions_for_face(make_face(make_photo("b"), unit(1, 0), carmen)) == []


def test_person_already_in_photo_is_not_suggested(people):
    carmen, _ = people
    photo = make_photo("q")
    make_face(photo, unit(1, 0), carmen)
    face = make_face(photo, unit(1, 0.1))

    assert similarity.suggestions_for_face(face) == []


def test_person_already_proposed_for_face_is_not_suggested(people, alice):
    carmen, _ = people
    face = make_face(make_photo("q"), unit(1, 0))
    voting.create_proposal(alice, face.photo, Proposal.Kind.FACE_IDENTITY, face=face, person=carmen)

    assert similarity.suggestions_for_face(face) == []


def test_removed_or_discarded_reference_faces_are_ignored(people):
    carmen, paco = people
    Face.objects.filter(person=paco).update(status=Face.Status.REMOVED)
    Photo.objects.filter(faces__person=carmen).update(discarded=True)

    assert similarity.suggestions_for_face(make_face(make_photo("q"), unit(1, 1))) == []


def test_candidate_faces_for_person(people):
    carmen, _ = people
    close = make_face(make_photo("c1"), unit(1, 0.05))
    farther = make_face(make_photo("c2"), unit(1, 0.6))
    make_face(make_photo("c3"), unit(0, 1))  # looks like Paco
    discarded = make_face(make_photo("c4"), unit(1, 0))
    Photo.objects.filter(pk=discarded.photo_id).update(discarded=True)

    candidates = similarity.candidate_faces_for_person(carmen)

    assert [c.face for c in candidates] == [close, farther]


def test_candidates_skip_faces_already_proposed_for_person(people, alice):
    carmen, _ = people
    face = make_face(make_photo("c1"), unit(1, 0))
    voting.create_proposal(alice, face.photo, Proposal.Kind.FACE_IDENTITY, face=face, person=carmen)

    assert similarity.candidate_faces_for_person(carmen) == []


def test_person_without_embeddings_has_no_candidates(db):
    assert similarity.candidate_faces_for_person(Person.objects.create(display_name="X")) == []


# --- Views ---------------------------------------------------------------------


def test_photo_panel_shows_suggestions_and_accepts_them(client, people, alice, data_dirs):
    carmen, _ = people
    face = make_face(make_photo("q"), unit(1, 0.2))
    client.force_login(alice)

    content = client.get(reverse("photo_detail", args=[face.photo_id])).content.decode()
    assert "¿Es Abuela Carmen?" in content

    client.post(reverse("propose_identity", args=[face.pk]), {"person_id": carmen.pk})
    proposal = Proposal.objects.get(face=face)
    assert proposal.person == carmen
    assert proposal.votes.get().user == alice


def test_person_page_lists_candidates_and_redirects_back(client, people, alice, data_dirs):
    carmen, _ = people
    face = make_face(make_photo("c1"), unit(1, 0))
    client.force_login(alice)
    url = reverse("person_detail", args=[carmen.pk])

    response = client.get(url)
    assert [c.face for c in response.context["candidates"]] == [face]

    response = client.post(
        reverse("propose_identity", args=[face.pk]), {"person_id": carmen.pk, "next": url}
    )
    assert response.url == url
    assert Proposal.objects.filter(face=face, person=carmen).exists()


def test_external_next_url_is_ignored(client, people, alice):
    carmen, _ = people
    face = make_face(make_photo("c1"), unit(1, 0))
    client.force_login(alice)

    response = client.post(
        reverse("propose_identity", args=[face.pk]),
        {"person_id": carmen.pk, "next": "https://evil.example/"},
    )
    assert "evil" not in response.url
