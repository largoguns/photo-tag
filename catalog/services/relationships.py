"""Relationship proposals and voting (spec section 5.12).

Relationships between persons are proposed and voted like names (section 5.4). The
accepted ones live in `Relationship`, stored once in canonical form (see `kinship`).

A family relationship occupies the *field* of its pair of persons (one family relationship
per pair); a social one, the field of its pair and kind. A proposal with a different value
for an accepted field reopens it: the accepted value stays until the new one is accepted.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext as _

from accounts.services import audit
from catalog.models import (
    ASYMMETRIC_KINDS,
    CatalogSettings,
    Person,
    Relationship,
    RelationshipKind,
    RelationshipProposal,
    RelationshipVote,
    Vote,
)
from catalog.services import kinship, voting
from catalog.services.names import name_key, normalize_label

Status = RelationshipProposal.Status
Action = RelationshipProposal.Action
K = RelationshipKind


def field_filter(person_from, person_to, kind):
    """Q for the relationships (or proposals) competing for the same field."""
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
        raise ValidationError(_("Esa relación ya está aceptada."))
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


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------


@transaction.atomic
def propose(user, subject, role, *, other=None, other_name="", activity=""):
    """Propose '<other> is <role> of <subject>'. `other` may be a name: an unknown name
    creates a provisional person, as with face names."""
    if role not in kinship.ROLES:
        raise ValidationError(_("Tipo de relación no válido."))
    if other is None:
        other = voting.resolve_person(other_name or "", user)
    subject, other = subject.resolved(), other.resolved()
    if subject.pk == other.pk:
        raise ValidationError(_("Una persona no puede relacionarse consigo misma."))
    _lock(subject, other)
    person_from, person_to, kind = kinship.canonical(subject.pk, role, other.pk)
    activity = normalize_label(activity)[:100] if kind == K.ACTIVITY_MATE else ""
    if kind == K.ACTIVITY_MATE and not activity:
        raise ValidationError(_("Escribe qué actividad comparten."))

    duplicate = next(
        (
            p
            for p in RelationshipProposal.objects.filter(
                person_from=person_from,
                person_to=person_to,
                kind=kind,
                action=Action.ADD,
                status=Status.OPEN,
            )
            if _same_value(p, kind, activity)
        ),
        None,
    )
    if duplicate:
        return cast_vote(user, duplicate, Vote.UP)

    validate(person_from, person_to, kind, activity)
    proposal = RelationshipProposal.objects.create(
        person_from_id=person_from,
        person_to_id=person_to,
        kind=kind,
        activity=activity,
        author=user,
    )
    return _open(user, proposal)


@transaction.atomic
def propose_removal(user, relationship):
    """Propose deleting an accepted relationship."""
    relationship = Relationship.objects.select_for_update().get(pk=relationship.pk)
    duplicate = RelationshipProposal.objects.filter(
        relationship=relationship, action=Action.REMOVE, status=Status.OPEN
    ).first()
    if duplicate:
        return cast_vote(user, duplicate, Vote.UP)
    proposal = RelationshipProposal.objects.create(
        person_from_id=relationship.person_from_id,
        person_to_id=relationship.person_to_id,
        kind=relationship.kind,
        activity=relationship.activity,
        action=Action.REMOVE,
        relationship=relationship,
        author=user,
    )
    return _open(user, proposal)


def _open(user, proposal):
    """The author's +1, audit and evaluation of a just created proposal."""
    RelationshipVote.objects.create(proposal=proposal, user=user, value=Vote.UP)
    audit.log(
        user,
        "relationship.propose",
        proposal,
        proposal_action=proposal.action,
        kind=proposal.kind,
        label=proposal.label(),
    )
    evaluate(proposal, user)
    proposal.refresh_from_db()
    return proposal


# ---------------------------------------------------------------------------
# Voting
# ---------------------------------------------------------------------------


@transaction.atomic
def cast_vote(user, proposal, value):
    if value not in (Vote.UP, Vote.DOWN):
        raise ValidationError(_("Voto no válido."))
    proposal = RelationshipProposal.objects.select_for_update().get(pk=proposal.pk)
    if not proposal.is_open:
        raise ValidationError(_("La propuesta ya está cerrada."))
    RelationshipVote.objects.update_or_create(
        proposal=proposal, user=user, defaults={"value": value}
    )
    audit.log(user, "relationship.vote", proposal, value=value)
    evaluate(proposal, user)
    proposal.refresh_from_db()
    return proposal


def tally(proposal):
    ups = proposal.votes.filter(value=Vote.UP).count()
    downs = proposal.votes.filter(value=Vote.DOWN).count()
    return ups, downs


def evaluate(proposal, user=None):
    if not proposal.is_open:
        return proposal.status
    conf = CatalogSettings.load()
    ups, downs = tally(proposal)
    result = voting.outcome(ups, downs, conf.vote_min_approvals, conf.vote_approval_ratio)
    if result == Status.ACCEPTED:
        _accept(proposal, user)
    elif result == Status.REJECTED:
        _reject(proposal)
    return proposal.status


@transaction.atomic
def admin_decide(admin, proposal, accept):
    """An administrator accepts or rejects a relationship proposal directly."""
    if not admin.is_catalog_admin:
        raise PermissionDenied
    proposal = RelationshipProposal.objects.select_for_update().get(pk=proposal.pk)
    if not proposal.is_open:
        raise ValidationError(_("La propuesta ya está cerrada."))
    proposal.decided_by = admin
    if accept:
        _accept(proposal, admin)
    else:
        _reject(proposal)
    action = "relationship.admin_accept" if accept else "relationship.admin_reject"
    audit.log(admin, action, proposal)
    return proposal


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


def _close(proposal, status):
    proposal.status = status
    proposal.decided_at = timezone.now()
    proposal.save(update_fields=["status", "decided_at", "decided_by"])


def _cleanup_persons(proposal):
    for person in (proposal.person_from, proposal.person_to):
        voting.cleanup_provisional_person(person)
    proposal.refresh_from_db(fields=["person_from", "person_to"])


def _supersede(proposals):
    for other in proposals:
        _close(other, Status.SUPERSEDED)
        _cleanup_persons(other)


def _accept(proposal, user):
    _lock(proposal.person_from, proposal.person_to)
    if proposal.action == Action.REMOVE:
        _accept_removal(proposal, user)
    else:
        _accept_addition(proposal)


def _accept_addition(proposal):
    person_from, person_to = proposal.person_from_id, proposal.person_to_id
    try:
        # The relationships may have changed while the proposal was open.
        validate(person_from, person_to, proposal.kind, proposal.activity)
    except ValidationError:
        _close(proposal, Status.SUPERSEDED)
        _cleanup_persons(proposal)
        return

    field = field_filter(person_from, person_to, proposal.kind)
    replaced = list(Relationship.objects.filter(field))
    _supersede(
        RelationshipProposal.objects.filter(field, status__in=[Status.OPEN, Status.ACCEPTED])
        .filter(Q(action=Action.ADD) | Q(relationship__in=replaced))
        .exclude(pk=proposal.pk)
    )
    for relationship in replaced:
        relationship.delete()
    Relationship.objects.create(
        person_from_id=person_from,
        person_to_id=person_to,
        kind=proposal.kind,
        activity=proposal.activity,
        accepted_proposal=proposal,
    )
    Person.objects.filter(pk__in=[person_from, person_to], is_provisional=True).update(
        is_provisional=False
    )
    _close(proposal, Status.ACCEPTED)


def _accept_removal(proposal, user):
    relationship = proposal.relationship
    if relationship is None:  # already gone
        _close(proposal, Status.SUPERSEDED)
        return
    _close(proposal, Status.ACCEPTED)
    others = RelationshipProposal.objects.filter(
        Q(relationship=relationship, status=Status.OPEN)
        | Q(pk=relationship.accepted_proposal_id, status=Status.ACCEPTED)
    ).exclude(pk=proposal.pk)
    for other in others:
        _close(other, Status.SUPERSEDED)
    audit.log(user, "relationship.delete", proposal, label=str(relationship))
    relationship.delete()


def _reject(proposal):
    _close(proposal, Status.REJECTED)
    _cleanup_persons(proposal)


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
    the same field, the target's one is kept. Open proposals move too unless they end up
    invalid or duplicated, in which case they are superseded; closed ones stay as history.
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

    for proposal in RelationshipProposal.objects.filter(involving, status=Status.OPEN):
        a, b = _moved(proposal, source.pk, target.pk)
        duplicate = (
            RelationshipProposal.objects.filter(
                person_from=a,
                person_to=b,
                kind=proposal.kind,
                action=proposal.action,
                status=Status.OPEN,
            )
            .exclude(pk=proposal.pk)
            .exists()
        )
        if a == b or duplicate:
            _close(proposal, Status.SUPERSEDED)
            continue
        proposal.person_from_id, proposal.person_to_id = a, b
        proposal.save(update_fields=["person_from", "person_to"])
