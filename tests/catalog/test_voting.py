import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from accounts.models import AuditLog
from catalog.models import CatalogSettings, Face, Person, Photo, Proposal, Vote
from catalog.services import voting
from catalog.services.persons import merge_persons

pytestmark = pytest.mark.django_db

Kind = Proposal.Kind
Status = Proposal.Status


def identity(user, face, name):
    return voting.create_proposal(user, face.photo, Kind.FACE_IDENTITY, face=face, person_name=name)


@pytest.mark.parametrize(
    ("ups", "downs", "expected"),
    [
        (1, 0, None),
        (2, 0, Status.ACCEPTED),
        (2, 1, Status.ACCEPTED),  # 0.667 >= 0.66
        (2, 2, None),
        (3, 2, None),  # 0.6 < 0.66
        (0, 2, Status.REJECTED),
        (1, 2, Status.REJECTED),
        (2, 3, Status.REJECTED),
        (1, 1, None),
    ],
)
def test_outcome_rule(ups, downs, expected):
    assert voting.outcome(ups, downs, 2, 0.66) == expected


def test_creating_a_proposal_counts_as_authors_vote(photo, alice):
    face = photo.faces.first()
    proposal = identity(alice, face, "Abuela Carmen")

    assert proposal.status == Status.OPEN
    assert list(proposal.votes.values_list("user__username", "value")) == [("alice", 1)]
    assert Person.objects.get(display_name="Abuela Carmen").is_provisional
    photo.refresh_from_db()
    assert photo.status == Photo.Status.IN_PROGRESS


def test_two_users_voting_same_name_accept_it(photo, alice, bob):
    """Acceptance criterion 2."""
    face = photo.faces.first()
    proposal = identity(alice, face, "Abuela Carmen")

    proposal = voting.cast_vote(bob, proposal, Vote.UP)

    assert proposal.status == Status.ACCEPTED
    face.refresh_from_db()
    assert face.person.display_name == "Abuela Carmen"
    assert not face.person.is_provisional


def test_same_name_proposed_twice_adds_a_vote(photo, alice, bob):
    face = photo.faces.first()
    identity(alice, face, "Abuela Carmen")

    proposal = identity(bob, face, "abuela  carmen")

    assert Proposal.objects.count() == 1
    assert proposal.status == Status.ACCEPTED


def test_rejection_deletes_provisional_person(photo, alice, bob, carol):
    face = photo.faces.first()
    proposal = identity(alice, face, "Tío Inventado")

    voting.cast_vote(bob, proposal, Vote.DOWN)
    proposal = voting.cast_vote(carol, proposal, Vote.DOWN)

    assert proposal.status == Status.REJECTED
    assert not Person.objects.filter(display_name="Tío Inventado").exists()


def test_rejection_keeps_person_with_other_references(photo, alice, bob, carol):
    first, second = photo.faces.all()
    identity(alice, first, "Prima Lola")
    proposal = identity(alice, second, "Prima Lola")

    voting.cast_vote(bob, proposal, Vote.DOWN)
    voting.cast_vote(carol, proposal, Vote.DOWN)

    assert Person.objects.filter(display_name="Prima Lola").exists()


def test_concurrent_proposals_loser_is_superseded(photo, alice, bob, carol):
    face = photo.faces.first()
    luis = identity(alice, face, "Luis")
    pedro = identity(bob, face, "Pedro")

    voting.cast_vote(carol, pedro, Vote.UP)

    luis.refresh_from_db()
    pedro.refresh_from_db()
    assert pedro.status == Status.ACCEPTED
    assert luis.status == Status.SUPERSEDED
    assert not Person.objects.filter(display_name="Luis").exists()


def test_reopening_keeps_previous_value_until_new_one_is_accepted(photo, alice, bob, carol):
    face = photo.faces.first()
    first = identity(alice, face, "Luis")
    voting.cast_vote(bob, first, Vote.UP)
    face.refresh_from_db()
    luis = face.person

    second = identity(carol, face, "Luis Alberto")
    face.refresh_from_db()
    assert face.person == luis
    assert second.status == Status.OPEN

    voting.cast_vote(alice, second, Vote.UP)
    face.refresh_from_db()
    first.refresh_from_db()
    assert face.person.display_name == "Luis Alberto"
    assert first.status == Status.SUPERSEDED


def test_proposing_the_accepted_value_is_rejected(photo, alice, bob):
    face = photo.faces.first()
    voting.cast_vote(bob, identity(alice, face, "Luis"), Vote.UP)

    with pytest.raises(ValidationError):
        identity(alice, face, "Luis")


def test_admin_can_accept_directly_and_it_is_logged(photo, alice, admin_user, boda):
    proposal = voting.create_proposal(alice, photo, Kind.EVENT, event_type=boda)

    voting.admin_decide(admin_user, proposal, accept=True)

    proposal.refresh_from_db()
    photo.refresh_from_db()
    assert proposal.status == Status.ACCEPTED
    assert proposal.decided_by == admin_user
    assert photo.event_type == boda
    assert AuditLog.objects.filter(action="proposal.admin_accept", user=admin_user).exists()


def test_admin_can_reject_directly(photo, alice, admin_user):
    proposal = identity(alice, photo.faces.first(), "Nadie")

    voting.admin_decide(admin_user, proposal, accept=False)

    proposal.refresh_from_db()
    assert proposal.status == Status.REJECTED


def test_collaborator_cannot_decide(photo, alice, bob):
    proposal = identity(alice, photo.faces.first(), "Nadie")

    with pytest.raises(PermissionDenied):
        voting.admin_decide(bob, proposal, accept=True)


def test_closed_proposals_cannot_be_voted(photo, alice, bob, carol):
    proposal = identity(alice, photo.faces.first(), "Luis")
    voting.cast_vote(bob, proposal, Vote.UP)

    with pytest.raises(ValidationError):
        voting.cast_vote(carol, proposal, Vote.DOWN)


def test_changing_a_vote_updates_it(photo, alice, bob):
    proposal = identity(alice, photo.faces.first(), "Luis")
    voting.cast_vote(bob, proposal, Vote.DOWN)
    voting.cast_vote(bob, proposal, Vote.UP)

    assert proposal.votes.filter(user=bob).get().value == Vote.UP


def test_thresholds_are_configurable(photo, alice, bob):
    conf = CatalogSettings.load()
    conf.vote_min_approvals = 3
    conf.save()
    proposal = identity(alice, photo.faces.first(), "Luis")

    proposal = voting.cast_vote(bob, proposal, Vote.UP)

    assert proposal.status == Status.OPEN


def test_photo_becomes_cataloged_with_all_faces_and_event(photo, alice, bob, boda):
    first, second = photo.faces.all()
    voting.cast_vote(bob, identity(alice, first, "Luis"), Vote.UP)
    voting.cast_vote(bob, identity(alice, second, "Ana"), Vote.UP)
    photo.refresh_from_db()
    assert photo.status == Photo.Status.IN_PROGRESS

    event = voting.create_proposal(
        alice, photo, Kind.EVENT, event_type=boda, value={"description": "Boda de Luis y Ana"}
    )
    voting.cast_vote(bob, event, Vote.UP)

    photo.refresh_from_db()
    assert photo.status == Photo.Status.CATALOGED
    assert photo.event_description == "Boda de Luis y Ana"
    assert photo.catalog_changed_at is not None


def test_remove_face_excludes_it_from_cataloging(photo, alice, bob, boda):
    first, second = photo.faces.all()
    voting.cast_vote(bob, identity(alice, first, "Luis"), Vote.UP)
    voting.cast_vote(
        bob, voting.create_proposal(alice, photo, Kind.EVENT, event_type=boda), Vote.UP
    )
    pending_name = identity(alice, second, "Fantasma")

    removal = voting.create_proposal(alice, photo, Kind.REMOVE_FACE, face=second)
    voting.cast_vote(bob, removal, Vote.UP)

    second.refresh_from_db()
    pending_name.refresh_from_db()
    photo.refresh_from_db()
    assert second.status == Face.Status.REMOVED
    assert pending_name.status == Status.SUPERSEDED
    assert photo.status == Photo.Status.CATALOGED


def test_add_face_flow(photo, alice, bob):
    proposal = voting.create_proposal(
        alice, photo, Kind.ADD_FACE, value={"x": 300, "y": 50, "w": 70, "h": 90}
    )
    face = proposal.face
    assert face.status == Face.Status.PROPOSED
    assert face.source == Face.Source.MANUAL
    assert face.created_by == alice

    voting.cast_vote(bob, proposal, Vote.UP)

    face.refresh_from_db()
    assert face.status == Face.Status.ACTIVE


def test_rejected_add_face_hides_the_face(photo, alice, bob, carol):
    proposal = voting.create_proposal(
        alice, photo, Kind.ADD_FACE, value={"x": 300, "y": 50, "w": 70, "h": 90}
    )
    voting.cast_vote(bob, proposal, Vote.DOWN)
    voting.cast_vote(carol, proposal, Vote.DOWN)

    proposal.face.refresh_from_db()
    assert proposal.face.status == Face.Status.REJECTED


def test_add_face_box_is_clamped_and_validated(photo, alice):
    proposal = voting.create_proposal(
        alice, photo, Kind.ADD_FACE, value={"x": 780, "y": 580, "w": 100, "h": 100}
    )
    assert (proposal.face.w, proposal.face.h) == (20, 20)
    with pytest.raises(ValidationError):
        voting.create_proposal(alice, photo, Kind.ADD_FACE, value={"x": 1, "y": 1, "w": 2, "h": 2})


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"precision": "year", "year": "1985"}, {"precision": "year", "year": 1985}),
        ({"precision": "decade", "year": "1987"}, {"precision": "decade", "year": 1980}),
        (
            {"precision": "exact", "year": "1985", "month": "7", "day": "14"},
            {"precision": "exact", "year": 1985, "month": 7, "day": 14},
        ),
    ],
)
def test_date_proposals(photo, alice, bob, value, expected):
    proposal = voting.create_proposal(alice, photo, Kind.DATE, value=value)
    assert proposal.value == expected

    voting.cast_vote(bob, proposal, Vote.UP)
    photo.refresh_from_db()
    assert photo.date_precision == expected["precision"]
    assert photo.date_value.year == expected["year"]


@pytest.mark.parametrize(
    "value",
    [
        {"precision": "year", "year": "abc"},
        {"precision": "year", "year": "1500"},
        {"precision": "exact", "year": "1985", "month": "2", "day": "30"},
        {"precision": "week", "year": "1985"},
    ],
)
def test_invalid_dates(photo, alice, value):
    with pytest.raises(ValidationError):
        voting.create_proposal(alice, photo, Kind.DATE, value=value)


def test_pending_for_user(photo, alice, bob):
    proposal = identity(alice, photo.faces.first(), "Luis")

    assert list(voting.pending_for_user(bob)) == [proposal]
    assert list(voting.pending_for_user(alice)) == []
    assert list(voting.photos_pending_for_user(bob)) == [photo]


def test_every_action_is_audited(photo, alice, bob):
    proposal = identity(alice, photo.faces.first(), "Luis")
    voting.cast_vote(bob, proposal, Vote.UP)

    actions = set(AuditLog.objects.values_list("action", flat=True))
    assert {"proposal.create", "vote.cast"} <= actions


def test_merge_persons_moves_faces_and_proposals(photo, alice, bob, admin_user):
    first, second = photo.faces.all()
    voting.cast_vote(bob, identity(alice, first, "Luis"), Vote.UP)
    # A duplicate created under another name (e.g. a nickname) is merged by an admin.
    voting.cast_vote(bob, identity(alice, second, "Luisito"), Vote.UP)
    luis, luis_dup = (
        Person.objects.get(display_name="Luis"),
        Person.objects.get(display_name="Luisito"),
    )

    merge_persons(admin_user, luis_dup, luis)

    second.refresh_from_db()
    luis_dup.refresh_from_db()
    assert second.person == luis
    assert luis_dup.merged_into == luis
    assert luis_dup not in Person.objects.active()
    assert AuditLog.objects.filter(action="person.merge").exists()
    # Proposing the merged name now resolves to the surviving person.
    assert voting.resolve_person("Luis", alice) == luis


def test_merge_with_itself_is_refused(photo, admin_user):
    person = Person.objects.create(display_name="Luis")
    with pytest.raises(ValidationError):
        merge_persons(admin_user, person, person)


@pytest.mark.parametrize("typed", ["luis", "LUIS", "Luís", "  luis  "])
def test_existing_person_is_reused_ignoring_case_and_accents(photo, alice, typed):
    existing = Person.objects.create(display_name="Luis")
    assert voting.resolve_person(typed, alice) == existing


def test_new_person_names_are_normalized(photo, alice):
    person = voting.resolve_person("maría josé DE LA cruz", alice)
    assert person.display_name == "María José de la Cruz"
    assert person.is_provisional
