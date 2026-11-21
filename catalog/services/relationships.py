"""Relationships between persons (spec section 5.12).

Any collaborator adds or removes a relationship directly, without a vote: people know
who their parents, children or partners are. Every change is audited. Relationships are
stored once in canonical form (see `kinship`).

A family relationship occupies the *field* of its pair of persons (one family relationship
per pair); a social one, the field of its pair and kind. Adding a different value for an
occupied field replaces it (Partner → Ex-partner, another activity).
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils.translation import gettext as _

from accounts.services import audit
from catalog.models import ASYMMETRIC_KINDS, Person, Relationship, RelationshipKind
from catalog.services import kinship, voting
from catalog.services.names import name_key, normalize_label

K = RelationshipKind


def field_filter(person_from, person_to, kind):
    """Q for the relationships competing for the same field."""
    if kind in kinship.FAMILY_KINDS:
        pair = Q(person_from=person_from, person_to=person_to) | Q(
            person_from=person_to, person_to=person_from
        )
        return pair & Q(kind__in=kinship.FAMILY_KINDS)
    return Q(person_from=person_from, person_to=person_to, kind=kind)


def _same_value(obj, kind, activity):
    return obj.kind == kind and name_key(obj.activity) == name_key(activity)


def _lock(*persons):
    ids = sorted({p.pk for p in persons})
    list(Person.objects.select_for_update().filter(pk__in=ids).order_by("pk"))


def validate(person_from, person_to, kind, activity=""):
    """Raise ValidationError if the relationship cannot be added (ids in canonical form)."""
    if person_from == person_to:
        raise ValidationError(_("Una persona no puede relacionarse consigo misma."))
    current = Relationship.objects.filter(field_filter(person_from, person_to, kind)).first()
    if current and _same_value(current, kind, activity):
        raise ValidationError(_("Esa relación ya está guardada."))
    if kind not in kinship.FAMILY_KINDS:
        return
    # The pair's current family relationship would be replaced: ignore it.
    graph = kinship.Graph.load(exclude_family_pair=(person_from, person_to))
    if kind == K.PARENT and len(graph.parents[person_to] - {person_from}) >= 2:
        raise ValidationError(_("Esa persona ya tiene dos progenitores."))
    if kind in kinship.ANCESTRY_KINDS and person_to in graph.ancestors(person_from):
        raise ValidationError(_("Nadie puede ser antepasado de uno de sus antepasados."))
    deduced = graph.deduced(person_to).get(person_from)
    if deduced:
        proposed = kinship.role_of_other(kind, viewer_is_from=False)
        if deduced == proposed:
            raise ValidationError(_("Esa relación ya se deduce de las demás."))
        raise ValidationError(
            _("Contradice una relación que ya se deduce: %(role)s.")
            % {"role": str(kinship.role_label(deduced)).lower()}
        )


@transaction.atomic
def add(user, subject, role, *, other=None, other_name="", new_person=False, activity=""):
    """Save '<other> is <role> of <subject>'. An unknown name creates the person (relatives
    without photos, such as great-grandparents). Returns the new relationship."""
    if role not in kinship.ROLES:
        raise ValidationError(_("Tipo de relación no válido."))
    subject = subject.resolved()
    if subject.is_provisional:
        raise ValidationError(_("Solo se puede hacer con personas ya aceptadas en el álbum."))
    if other is None:
        other = voting.resolve_person(other_name or "", user, new=new_person)
    other = other.resolved()
    if subject.pk == other.pk:
        raise ValidationError(_("Una persona no puede relacionarse consigo misma."))
    _lock(subject, other)
    person_from, person_to, kind = kinship.canonical(subject.pk, role, other.pk)
    activity = normalize_label(activity)[:100] if kind == K.ACTIVITY_MATE else ""
    if kind == K.ACTIVITY_MATE and not activity:
        raise ValidationError(_("Escribe qué actividad comparten."))
    validate(person_from, person_to, kind, activity)

    replaced = [
        str(r) for r in Relationship.objects.filter(field_filter(person_from, person_to, kind))
    ]
    Relationship.objects.filter(field_filter(person_from, person_to, kind)).delete()
    if other.is_provisional:  # created just now from a name: a person of the album
        other.is_provisional = False
        other.save(update_fields=["is_provisional"])
    relationship = Relationship.objects.create(
        person_from_id=person_from,
        person_to_id=person_to,
        kind=kind,
        activity=activity,
        created_by=user,
    )
    details = {"kind": kind, "label": str(relationship)}
    if replaced:
        details["replaced"] = replaced
    audit.log(user, "relationship.add", relationship, **details)
    return relationship


@transaction.atomic
def remove(user, relationship):
    relationship = Relationship.objects.select_related("person_from", "person_to").get(
        pk=relationship.pk
    )
    audit.log(user, "relationship.delete", relationship, label=str(relationship))
    relationship.delete()


# ---------------------------------------------------------------------------
# Merging persons
# ---------------------------------------------------------------------------


def _moved(obj, source_id, target_id):
    """(from, to) of `obj` with the source replaced by the target, in canonical order."""
    a = target_id if obj.person_from_id == source_id else obj.person_from_id
    b = target_id if obj.person_to_id == source_id else obj.person_to_id
    if obj.kind not in ASYMMETRIC_KINDS:
        a, b = min(a, b), max(a, b)
    return a, b


def move_to(source, target):
    """Move the relationships of `source` to `target` when merging persons.

    Relationships with oneself are dropped and, when the target already has a value for
    the same field, the target's one is kept.
    """
    involving = Q(person_from=source) | Q(person_to=source)
    for relationship in Relationship.objects.filter(involving):
        a, b = _moved(relationship, source.pk, target.pk)
        clash = (
            a == b
            or Relationship.objects.filter(field_filter(a, b, relationship.kind))
            .exclude(pk=relationship.pk)
            .exists()
        )
        if clash:
            relationship.delete()
            continue
        relationship.person_from_id, relationship.person_to_id = a, b
        relationship.save(update_fields=["person_from", "person_to"])
