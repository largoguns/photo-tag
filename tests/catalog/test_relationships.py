import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import translation

from accounts.models import AuditLog
from catalog.models import Person, Relationship, RelationshipProposal, Vote
from catalog.services import relationships
from catalog.services.persons import merge_persons

pytestmark = pytest.mark.django_db

K = Relationship.Kind
Status = RelationshipProposal.Status


@pytest.fixture(autouse=True)
def spanish():
    """Labels and messages are checked in Spanish whatever an earlier test activated."""
    with translation.override("es"):
        yield


def person(name):
    return Person.objects.create(display_name=name)


def accepted(user1, user2, subject, role, other, **extra):
    proposal = relationships.propose(user1, subject, role, other=other, **extra)
    return relationships.cast_vote(user2, proposal, Vote.UP)


@pytest.fixture
def ana():
    return person("Ana")


@pytest.fixture
def luis():
    return person("Luis")


def test_proposal_counts_as_authors_vote_and_is_stored_canonically(alice, ana, luis):
    proposal = relationships.propose(alice, ana, "child", other=luis)  # Luis is Ana's child

    assert proposal.status == Status.OPEN
    assert (proposal.person_from, proposal.person_to, proposal.kind) == (ana, luis, K.PARENT)
    assert list(proposal.votes.values_list("user__username", "value")) == [("alice", 1)]
    assert proposal.label() == "Ana, progenitor de Luis"


def test_two_votes_accept_it_and_it_is_reciprocal(alice, bob, ana, luis):
    proposal = accepted(alice, bob, luis, "parent", ana)  # Ana is Luis's parent

    assert proposal.status == Status.ACCEPTED
    relationship = Relationship.objects.get()
    assert (relationship.person_from, relationship.person_to) == (ana, luis)
    assert relationship.accepted_proposal == proposal


def test_votes_against_reject_it(alice, bob, carol, ana, luis):
    proposal = relationships.propose(alice, ana, "sibling", other=luis)
    relationships.cast_vote(bob, proposal, Vote.DOWN)
    proposal = relationships.cast_vote(carol, proposal, Vote.DOWN)

    assert proposal.status == Status.REJECTED
    assert not Relationship.objects.exists()


def test_identical_open_proposal_adds_a_vote(alice, bob, ana, luis):
    first = relationships.propose(alice, ana, "cousin", other=luis)
    second = relationships.propose(bob, luis, "cousin", other=ana)

    assert second.pk == first.pk
    assert second.status == Status.ACCEPTED
    assert RelationshipProposal.objects.count() == 1


def test_admin_decides_directly(admin_user, alice, ana, luis):
    proposal = relationships.propose(alice, ana, "friend", other=luis)
    with pytest.raises(PermissionDenied):
        relationships.admin_decide(alice, proposal, True)

    proposal = relationships.admin_decide(admin_user, proposal, True)

    assert proposal.status == Status.ACCEPTED
    assert proposal.decided_by == admin_user
    assert Relationship.objects.filter(kind=K.FRIEND).exists()


def test_reopening_a_family_field_replaces_it_when_accepted(alice, bob, ana, luis):
    partner = accepted(alice, bob, ana, "partner", luis)
    reopen = relationships.propose(alice, ana, "ex_partner", other=luis)

    # The accepted value stays until the new one is accepted.
    assert Relationship.objects.get().kind == K.PARTNER
    relationships.cast_vote(bob, reopen, Vote.UP)

    assert Relationship.objects.get().kind == K.EX_PARTNER
    partner.refresh_from_db()
    assert partner.status == Status.SUPERSEDED


def test_competing_family_proposals_supersede_each_other(alice, bob, ana, luis):
    sibling = relationships.propose(alice, ana, "sibling", other=luis)
    cousin = relationships.propose(alice, ana, "cousin", other=luis)

    relationships.cast_vote(bob, cousin, Vote.UP)

    sibling.refresh_from_db()
    assert sibling.status == Status.SUPERSEDED
    assert Relationship.objects.get().kind == K.COUSIN


def test_social_relationships_add_up(alice, bob, ana, luis):
    accepted(alice, bob, ana, "cousin", luis)
    accepted(alice, bob, ana, "coworker", luis)
    accepted(alice, bob, ana, "neighbor", luis)

    assert set(Relationship.objects.values_list("kind", flat=True)) == {
        K.COUSIN,
        K.COWORKER,
        K.NEIGHBOR,
    }


def test_activity_is_required_normalized_and_can_be_changed(alice, bob, ana, luis):
    with pytest.raises(ValidationError):
        relationships.propose(alice, ana, "activity_mate", other=luis)

    accepted(alice, bob, ana, "activity_mate", luis, activity="  FÚTBOL ")
    assert Relationship.objects.get().activity == "Fútbol"

    with pytest.raises(ValidationError, match="ya está aceptada"):
        relationships.propose(alice, luis, "activity_mate", other=ana, activity="futbol")
    accepted(alice, bob, ana, "activity_mate", luis, activity="pádel")
    assert Relationship.objects.get().activity == "Pádel"


def test_removal_is_voted(alice, bob, ana, luis):
    addition = accepted(alice, bob, ana, "friend", luis)
    relationship = Relationship.objects.get()

    removal = relationships.propose_removal(alice, relationship)
    assert removal.label() == "Quitar: Ana y Luis: amigo"
    assert relationships.propose_removal(bob, relationship).pk == removal.pk

    removal.refresh_from_db()
    assert removal.status == Status.ACCEPTED
    assert not Relationship.objects.exists()
    addition.refresh_from_db()
    assert addition.status == Status.SUPERSEDED
    assert AuditLog.objects.filter(action="relationship.delete").exists()


def test_unknown_name_creates_a_provisional_person_removed_on_rejection(alice, bob, carol, ana):
    proposal = relationships.propose(alice, ana, "grandparent", other_name="bisabuelo pepe")
    pepe = Person.objects.get(display_name="Bisabuelo Pepe")
    assert pepe.is_provisional

    relationships.cast_vote(bob, proposal, Vote.DOWN)
    proposal = relationships.cast_vote(carol, proposal, Vote.DOWN)

    assert not Person.objects.filter(pk=pepe.pk).exists()
    # The proposal stays as history.
    assert proposal.status == Status.REJECTED
    assert proposal.label() == "(persona eliminada), abuelo de Ana"


def test_accepted_provisional_person_becomes_permanent(alice, bob, ana):
    proposal = relationships.propose(alice, ana, "grandparent", other_name="Pepe")
    relationships.cast_vote(bob, proposal, Vote.UP)

    assert not Person.objects.get(display_name="Pepe").is_provisional


def test_provisional_person_in_another_relationship_is_kept(alice, bob, carol, ana, luis):
    relationships.propose(alice, ana, "grandparent", other_name="Pepe")
    pepe = Person.objects.get(display_name="Pepe")
    other = relationships.propose(alice, luis, "grandparent", other=pepe)

    relationships.cast_vote(bob, other, Vote.DOWN)
    relationships.cast_vote(carol, other, Vote.DOWN)

    assert Person.objects.filter(pk=pepe.pk).exists()


def test_cannot_relate_a_person_with_oneself(alice, ana):
    with pytest.raises(ValidationError, match="consigo misma"):
        relationships.propose(alice, ana, "friend", other=ana)


def test_unknown_role(alice, ana, luis):
    with pytest.raises(ValidationError):
        relationships.propose(alice, ana, "boss", other=luis)


def test_at_most_two_parents(alice, bob, ana, luis):
    child = person("Hijo")
    accepted(alice, bob, child, "parent", ana)
    accepted(alice, bob, child, "parent", luis)

    with pytest.raises(ValidationError, match="dos progenitores"):
        relationships.propose(alice, child, "parent", other=person("Otro"))


def test_no_ancestry_cycles(alice, bob, ana, luis):
    accepted(alice, bob, luis, "parent", ana)  # Ana → Luis
    nieto = person("Nieto")
    accepted(alice, bob, nieto, "parent", luis)  # Luis → Nieto

    with pytest.raises(ValidationError, match="antepasado"):
        relationships.propose(alice, ana, "grandparent", other=nieto)


def test_deduced_relationships_cannot_be_proposed_or_contradicted(alice, bob, ana, luis):
    marta = person("Marta")
    accepted(alice, bob, luis, "parent", ana)
    accepted(alice, bob, marta, "parent", ana)

    with pytest.raises(ValidationError, match="ya se deduce"):
        relationships.propose(alice, luis, "sibling", other=marta)
    with pytest.raises(ValidationError, match="deduce: hermano"):
        relationships.propose(alice, luis, "cousin", other=marta)


def test_explicit_relationships_without_the_intermediate_link(alice, bob, ana, luis):
    # A father-in-law without the partner in the album is still allowed.
    proposal = accepted(alice, bob, ana, "parent_in_law", luis)
    assert proposal.status == Status.ACCEPTED


def test_acceptance_revalidates_the_relationship(alice, bob, ana, luis):
    child = person("Hijo")
    third = relationships.propose(alice, child, "parent", other=person("Tercero"))
    accepted(alice, bob, child, "parent", ana)
    accepted(alice, bob, child, "parent", luis)

    third = relationships.cast_vote(bob, third, Vote.UP)

    assert third.status == Status.SUPERSEDED
    assert Relationship.objects.filter(person_to=child).count() == 2


def test_audit_records_proposals_and_votes(alice, bob, ana, luis):
    accepted(alice, bob, ana, "friend", luis)
    actions = set(AuditLog.objects.values_list("action", flat=True))
    assert {"relationship.propose", "relationship.vote"} <= actions


def test_merge_moves_relationships_recanonicalizing_and_dropping_duplicates(admin_user, alice, bob):
    a, b, c = person("A"), person("B"), person("C")  # ids ascending
    accepted(alice, bob, b, "friend", c)  # (b, c)
    accepted(alice, bob, a, "friend", c)  # (a, c)
    accepted(alice, bob, b, "neighbor", a)  # (a, b) → (a, c) after merging b into c
    open_proposal = relationships.propose(alice, b, "cousin", other=person("D"))

    merge_persons(admin_user, b, c)

    rows = set(Relationship.objects.values_list("person_from", "person_to", "kind"))
    # (b, c) friends became (c, c): dropped. (a, c) neighbour kept, canonical order.
    assert rows == {(a.pk, c.pk, K.FRIEND), (a.pk, c.pk, K.NEIGHBOR)}
    open_proposal.refresh_from_db()
    assert open_proposal.status == Status.OPEN
    assert c.pk in (open_proposal.person_from_id, open_proposal.person_to_id)


def test_merge_keeps_the_targets_family_relationship(admin_user, alice, bob, ana, luis):
    twin = person("Luis bis")
    accepted(alice, bob, ana, "sibling", luis)
    accepted(alice, bob, ana, "cousin", twin)

    merge_persons(admin_user, twin, luis)

    assert list(Relationship.objects.values_list("kind", flat=True)) == [K.SIBLING]


def test_database_enforces_the_canonical_form(ana, luis):
    low, high = sorted((ana, luis), key=lambda p: p.pk)
    with pytest.raises(IntegrityError), transaction.atomic():
        Relationship.objects.create(person_from=high, person_to=low, kind=K.SIBLING)
    with pytest.raises(IntegrityError), transaction.atomic():
        Relationship.objects.create(person_from=ana, person_to=ana, kind=K.PARENT)
    Relationship.objects.create(person_from=low, person_to=high, kind=K.FRIEND)
    with pytest.raises(IntegrityError), transaction.atomic():
        Relationship.objects.create(person_from=low, person_to=high, kind=K.FRIEND)
    # Asymmetric kinds may go either way.
    Relationship.objects.create(person_from=high, person_to=low, kind=K.PARENT)
