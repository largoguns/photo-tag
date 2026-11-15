"""Main photo (avatar) of each person: the face chosen by the family or the largest one."""

from accounts.services import audit
from catalog.models import Face


def selectable_faces(person):
    """Faces that can be chosen as the main photo of `person`."""
    return (
        Face.objects.filter(person=person, status=Face.Status.ACTIVE, photo__discarded=False)
        .select_related("photo")
        .order_by("-w", "pk")
    )


def avatar_faces(person_ids):
    """{person_id: face_id}: the chosen face while it still shows the person, else the largest."""
    person_ids = list(person_ids)
    avatars = {}
    faces = (
        Face.objects.filter(
            person_id__in=person_ids, status=Face.Status.ACTIVE, photo__discarded=False
        )
        .order_by("person_id", "-w", "pk")
        .values_list("person_id", "pk", "person__avatar_face_id")
    )
    for person_id, face_id, chosen in faces:
        if face_id == chosen:
            avatars[person_id] = face_id
        else:
            avatars.setdefault(person_id, face_id)
    return avatars


def avatar_face_id(person):
    return avatar_faces([person.pk]).get(person.pk)


def set_avatar(user, person, face_id):
    """Choose the main photo of `person`. Raises ValueError for a face that is not theirs."""
    face = selectable_faces(person).filter(pk=face_id).first()
    if face is None:
        raise ValueError("Face does not show this person")
    person.avatar_face = face
    person.save(update_fields=["avatar_face"])
    audit.log(user, "person.avatar", person, face=face.pk)
    return face
