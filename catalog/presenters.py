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
        "location_proposals": [p for p in open_proposals if p.kind == Proposal.Kind.LOCATION],
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


def person_avatars(person_ids):
    """{person_id: face_id} with the main photo of each person."""
    from catalog.services.avatars import avatar_faces

    return avatar_faces(person_ids)


def person_nicknames(person_ids):
    """{person_id: [nickname, ...]} of the given people."""
    from catalog.models import PersonNickname

    result = {}
    rows = PersonNickname.objects.filter(person_id__in=person_ids).values_list("person_id", "text")
    for person_id, text in rows:
        result.setdefault(person_id, []).append(text)
    return result


def proposal_activity(proposals, params, viewer):
    """Context for a filterable, paginated list of proposals (Mi actividad, audit screen)."""
    from django.core.paginator import Paginator

    from catalog.models import Proposal

    proposals = with_tally(proposals, viewer).select_related("photo")
    status, kind = params.get("status", ""), params.get("kind", "")
    if status in Proposal.Status.values:
        proposals = proposals.filter(status=status)
    if kind in Proposal.Kind.values:
        proposals = proposals.filter(kind=kind)
    page = Paginator(proposals.order_by("-created_at"), 30).get_page(params.get("page"))
    return {
        "page": page,
        "params": params,
        "statuses": Proposal.Status.choices,
        "kinds": Proposal.Kind.choices,
        "selected_status": Proposal.Status(status).label
        if status in Proposal.Status.values
        else None,
        "selected_kind": Proposal.Kind(kind).label if kind in Proposal.Kind.values else None,
    }


def family_milestone(percent):
    """The collective goal: a message at 25, 50, 75 and 100 % of the album catalogued."""
    from django.utils.translation import gettext as _

    if percent >= 100:
        return _("¡La familia ha catalogado todo el álbum!")
    if percent >= 75:
        return _("¡Ya tenéis tres cuartas partes del álbum catalogadas!")
    if percent >= 50:
        return _("¡La familia ha catalogado la mitad del álbum!")
    if percent >= 25:
        return _("¡Un cuarto del álbum ya está catalogado!")
    return ""


def people_json(exclude=None):
    """Active people for the name autocomplete: `name` is the value sent (matched by
    voting.resolve_person); `shown` and `nicknames` are for display and search. Avatars
    are face crops behind the login."""
    from django.urls import reverse

    from catalog.models import Person

    people = list(
        Person.objects.active()
        .exclude(pk=exclude)
        .order_by("display_name")
        .only("pk", "display_name", "honorific")
    )
    ids = [person.pk for person in people]
    avatars = person_avatars(ids)
    nicknames = person_nicknames(ids)
    face_url = reverse("face_image", args=[0]).replace("/0/", "/{id}/")
    return [
        {
            "name": person.display_name,
            "shown": person.shown_name,
            "nicknames": nicknames.get(person.pk, []),
            "avatar": face_url.replace("{id}", str(avatars[person.pk]))
            if person.pk in avatars
            else "",
        }
        for person in people
    ]


def person_sheet_context(person):
    """Nicknames and relationships shown in the person sheet."""
    from django.utils.translation import gettext as _

    from catalog.services import kinship

    relations = kinship.relations_for(person)
    avatars = person_avatars([relation.person.pk for relation in relations])
    for relation in relations:
        relation.avatar_id = avatars.get(relation.person.pk)
    family = [r for r in relations if r.tree == kinship.FAMILY]
    social = [r for r in relations if r.tree == kinship.SOCIAL]
    return {
        "nicknames": list(person.nicknames.all()),
        "family_relations": family,
        "social_relations": social,
        "relation_groups": [(_("Familia"), family), (_("Relaciones"), social)],
        "family_roles": [(role, kinship.role_label(role)) for role in kinship.FAMILY_ROLES],
        "social_roles": [(role, kinship.role_label(role)) for role in kinship.SOCIAL_ROLES],
    }
