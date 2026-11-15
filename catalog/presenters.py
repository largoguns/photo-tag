"""Query helpers that shape catalog data for templates."""

from django.db.models import Count, OuterRef, Q, Subquery

from catalog.models import Face, Photo, Proposal, Vote
from catalog.services.similarity import IdentityIndex


def with_tally(proposals, user):
    return proposals.annotate(
        ups=Count("votes", filter=Q(votes__value=Vote.UP)),
        downs=Count("votes", filter=Q(votes__value=Vote.DOWN)),
        my_vote=Subquery(
            Vote.objects.filter(proposal=OuterRef("pk"), user=user).values("value")[:1]
        ),
    ).select_related("person", "event_type", "author", "decided_by")


def photo_context(photo, user, selected_face=None):
    open_proposals = list(
        with_tally(
            Proposal.objects.filter(photo=photo, status=Proposal.Status.OPEN), user
        ).order_by("created_at", "id")
    )
    faces = list(
        photo.faces.filter(status__in=[Face.Status.ACTIVE, Face.Status.PROPOSED])
        .select_related("person")
        .order_by("x", "id")
    )
    index = IdentityIndex() if any(not f.person_id for f in faces) else None
    by_face = {}
    for proposal in open_proposals:
        if proposal.face_id:
            by_face.setdefault(proposal.face_id, []).append(proposal)
    for number, face in enumerate(faces, start=1):
        face.number = number
        face.box = face.as_percent()
        face.open_proposals = by_face.get(face.pk, [])
        face.identity_proposals = [
            p for p in face.open_proposals if p.kind == Proposal.Kind.FACE_IDENTITY
        ]
        face.other_proposals = [
            p for p in face.open_proposals if p.kind != Proposal.Kind.FACE_IDENTITY
        ]
        face.suggestions = index.suggestions_for_face(face) if index else []
    return {
        "photo": photo,
        "faces": faces,
        "selected_face": selected_face,
        "event_proposals": [p for p in open_proposals if p.kind == Proposal.Kind.EVENT],
        "date_proposals": [p for p in open_proposals if p.kind == Proposal.Kind.DATE],
        "prev_photo": Photo.objects.visible().filter(pk__lt=photo.pk).order_by("-pk").first(),
        "next_photo": Photo.objects.visible().filter(pk__gt=photo.pk).order_by("pk").first(),
    }


def next_uncataloged(after=None):
    base = Photo.objects.visible().exclude(status=Photo.Status.CATALOGED).order_by("pk")
    if after is not None:
        photo = base.filter(pk__gt=after).first()
        if photo:
            return photo
    return base.first()


# ---------------------------------------------------------------------------
# Gallery: sections by year and filter chips
# ---------------------------------------------------------------------------


def gallery_group(photo):
    """(key, label) of the year section a photo belongs to."""
    from django.utils.translation import gettext

    if not photo.date_value:
        return "none", gettext("Sin fecha")
    if photo.date_precision == Photo.DatePrecision.DECADE:
        return f"d{photo.decade}", gettext("Década de %(decade)s") % {"decade": photo.decade}
    return f"y{photo.date_value.year}", str(photo.date_value.year)


def group_photos(photos, last_group=""):
    """Consecutive photos grouped by section; a page continuing a section has no header."""
    groups = []
    for photo in photos:
        key, label = gallery_group(photo)
        if not groups or groups[-1]["key"] != key:
            groups.append(
                {
                    "key": key,
                    "label": label,
                    "header": key != last_group or bool(groups),
                    "photos": [],
                }
            )
        groups[-1]["photos"].append(photo)
    return groups


def person_avatars(person_ids):
    """{person_id: face_id} with the largest accepted face of each person."""
    avatars = {}
    faces = (
        Face.objects.filter(person_id__in=person_ids, status=Face.Status.ACTIVE)
        .order_by("person_id", "-w")
        .values_list("person_id", "pk")
    )
    for person_id, face_id in faces:
        avatars.setdefault(person_id, face_id)
    return avatars
