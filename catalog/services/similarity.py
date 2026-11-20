"""Identity suggestions from face embeddings (spec section 3.5).

Only suggestions: accepting an identity always goes through a proposal and votes.
"""

from dataclasses import dataclass

import numpy as np
from django.conf import settings
from django.utils.translation import gettext as _

from catalog.models import Face, Person, Proposal

EMBEDDING_DIM = 128


@dataclass
class Suggestion:
    person: Person
    score: float

    @property
    def strong(self):
        return self.score >= settings.FACE_MATCH_STRONG

    @property
    def label(self):
        return _("muy probable") if self.strong else _("posible")


@dataclass
class Candidate:
    face: Face
    score: float


def _vector(data):
    return np.frombuffer(bytes(data), dtype=np.float32)


def _matrix(blobs):
    if not blobs:
        return np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
    return np.vstack([_vector(b) for b in blobs])


def _identified_faces():
    return Face.objects.filter(
        status=Face.Status.ACTIVE,
        person__isnull=False,
        embedding__isnull=False,
        photo__discarded=False,
    )


class IdentityIndex:
    """Embeddings of every accepted face, loaded once per request."""

    def __init__(self):
        rows = list(_identified_faces().values_list("person_id", "embedding"))
        self.person_ids = np.array([r[0] for r in rows], dtype=np.int64)
        self.matrix = _matrix([r[1] for r in rows])

    def best_by_person(self, vector):
        """{person_id: best cosine similarity} for one embedding."""
        if not len(self.person_ids):
            return {}
        scores = self.matrix @ vector
        best = {}
        for person_id, score in zip(self.person_ids.tolist(), scores.tolist(), strict=True):
            if score > best.get(person_id, -1.0):
                best[person_id] = score
        return best

    def suggestions_for_face(self, face, limit=3):
        if face.embedding is None or face.person_id:
            return []
        excluded = set(
            face.photo.faces.filter(status=Face.Status.ACTIVE, person__isnull=False)
            .exclude(pk=face.pk)
            .values_list("person_id", flat=True)
        )
        excluded |= set(
            Proposal.objects.filter(
                face=face, kind=Proposal.Kind.FACE_IDENTITY, status=Proposal.Status.OPEN
            ).values_list("person_id", flat=True)
        )
        ranked = sorted(
            (
                (score, person_id)
                for person_id, score in self.best_by_person(_vector(face.embedding)).items()
                if score >= settings.FACE_MATCH_THRESHOLD and person_id not in excluded
            ),
            reverse=True,
        )[:limit]
        people = Person.objects.in_bulk([person_id for _, person_id in ranked])
        return [
            Suggestion(person=people[person_id], score=round(score, 3))
            for score, person_id in ranked
            if person_id in people
        ]


def suggestions_for_face(face, limit=3):
    return IdentityIndex().suggestions_for_face(face, limit)


def _unknown_faces():
    return Face.objects.filter(
        status=Face.Status.ACTIVE,
        person__isnull=True,
        embedding__isnull=False,
        photo__discarded=False,
    )


def _candidates_by_person(person_ids):
    """{person_id: [(score, face_pk)]} of unidentified faces that look like each person.

    Skips faces in photos where the person already is and faces with an open identity
    proposal for that person. One query per table, whatever the number of people.
    """
    references = {}
    photos_with = {}
    for person_id, photo_id, embedding in (
        _identified_faces()
        .filter(person_id__in=person_ids)
        .values_list("person_id", "photo_id", "embedding")
    ):
        references.setdefault(person_id, []).append(embedding)
        photos_with.setdefault(person_id, set()).add(photo_id)
    if not references:
        return {}
    unknown = list(_unknown_faces().values_list("pk", "photo_id", "embedding"))
    if not unknown:
        return {}
    proposed = set(
        Proposal.objects.filter(
            person_id__in=references, kind=Proposal.Kind.FACE_IDENTITY, status=Proposal.Status.OPEN
        ).values_list("person_id", "face_id")
    )
    unknown_matrix = _matrix([row[2] for row in unknown])
    result = {}
    for person_id, embeddings in references.items():
        scores = (unknown_matrix @ _matrix(embeddings).T).max(axis=1)
        result[person_id] = [
            (float(score), face_pk)
            for score, (face_pk, photo_id, _) in zip(scores.tolist(), unknown, strict=True)
            if score >= settings.FACE_MATCH_THRESHOLD
            and photo_id not in photos_with[person_id]
            and (person_id, face_pk) not in proposed
        ]
    return result


def candidate_faces_for_person(person, limit=12):
    """Unidentified faces that look most like `person`, best first."""
    ranked = sorted(
        _candidates_by_person([person.pk]).get(person.pk, []), key=lambda pair: -pair[0]
    )[:limit]
    faces = Face.objects.select_related("photo").in_bulk([face_pk for _, face_pk in ranked])
    return [Candidate(face=faces[face_pk], score=round(score, 3)) for score, face_pk in ranked]


def candidate_counts(person_ids):
    """{person_id: number of candidate faces} for the persons list (only people with some)."""
    return {
        person_id: len(pairs)
        for person_id, pairs in _candidates_by_person(person_ids).items()
        if pairs
    }
