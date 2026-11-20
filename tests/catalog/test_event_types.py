"""«Otro…» events and new places go to an administrator: accept (as typed or rewritten),
reject or — events — turn into a location. They are never decided by votes."""

import importlib

import pytest
from django.apps import apps
from django.urls import reverse

from accounts.models import AuditLog
from catalog.models import EventType, Location, Photo, Proposal, Vote
from catalog.services import catalog_review, voting
from catalog.services.names import normalize_label

pytestmark = pytest.mark.django_db
Status = Proposal.Status


def propose(user, photo, name, description=""):
    return voting.create_proposal(
        user, photo, Proposal.Kind.EVENT, event_name=name, value={"description": description}
    )


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("graduación", "Graduación"),
        ("fin de CURSO", "Fin de CURSO"),
        ("FIESTA DEL PUEBLO", "Fiesta del pueblo"),
    ],
)
def test_normalize_label(typed, expected):
    assert normalize_label(typed) == expected


# --- Proposing ----------------------------------------------------------------------------


def test_other_creates_a_type_in_review_that_votes_do_not_decide(photo, alice, bob, carol):
    proposal = propose(alice, photo, "graduación")
    assert proposal.event_type.name == "Graduación" and proposal.event_type.is_provisional
    assert proposal.in_review
    voting.cast_vote(bob, proposal, Vote.UP)
    voting.cast_vote(carol, proposal, Vote.UP)
    proposal.refresh_from_db()
    assert proposal.status == Status.OPEN  # only an administrator decides
    assert proposal.event_type not in voting.event_types()  # not offered meanwhile


def test_in_review_is_not_pending_for_my_vote(photo, alice, bob):
    propose(alice, photo, "Graduación")
    assert not voting.pending_for_user(bob).exists()


def test_a_known_name_reuses_the_type_and_is_voted(photo, alice, bob, boda):
    proposal = propose(alice, photo, "BODA")
    assert proposal.event_type == boda and not proposal.in_review
    voting.cast_vote(bob, proposal, Vote.UP)
    proposal.refresh_from_db()
    assert proposal.status == Status.ACCEPTED


def test_an_admin_typing_a_new_event_adds_it(photo, admin_user):
    proposal = propose(admin_user, photo, "Graduación")
    assert proposal.status == Status.ACCEPTED
    assert not proposal.event_type.is_provisional


def test_photo_shows_in_review_without_votes(client, photo, alice, bob):
    propose(alice, photo, "Graduación")
    client.force_login(bob)
    content = client.get(reverse("photo_detail", args=[photo.pk])).content.decode()
    assert "En revisión" in content
    event_panel = content.split('id="event-panel"')[1].split("</details>")[0]
    assert "vote up" not in event_panel
    assert 'value="new"' in content and 'name="event_name"' in content


# --- Review -------------------------------------------------------------------------------


def test_accept_as_typed(photo, alice, admin_user):
    proposal = propose(alice, photo, "Graduación")
    catalog_review.accept(admin_user, proposal.event_type)
    proposal.refresh_from_db()
    photo.refresh_from_db()
    assert proposal.status == Status.ACCEPTED and proposal.decided_by == admin_user
    assert photo.event_type.name == "Graduación" and not photo.event_type.is_provisional
    assert AuditLog.objects.filter(action="event_type.accept").exists()


def test_accept_with_a_better_text_renames(photo, alice, admin_user):
    proposal = propose(alice, photo, "graduacion del cole")
    catalog_review.accept(admin_user, proposal.event_type, "Fiesta escolar")
    photo.refresh_from_db()
    assert photo.event_type.name == "Fiesta escolar"


def test_accept_with_an_existing_text_merges(photo, alice, admin_user, boda):
    proposal = propose(alice, photo, "Bodorrio")
    provisional = proposal.event_type
    catalog_review.accept(admin_user, provisional, "boda")
    photo.refresh_from_db()
    assert photo.event_type == boda
    assert not EventType.objects.filter(pk=provisional.pk).exists()


def test_reject(photo, alice, admin_user):
    proposal = propose(alice, photo, "Sábado noche")
    provisional = proposal.event_type
    catalog_review.reject(admin_user, provisional)
    proposal.refresh_from_db()
    assert proposal.status == Status.REJECTED
    assert not EventType.objects.filter(pk=provisional.pk).exists()
    assert proposal.label() == "Sábado noche"  # the name stays in the proposal


def test_reject_old_junk_already_on_photos(photo, alice, admin_user):
    junk = EventType.objects.create(name="Visita", created_by=alice, is_provisional=True)
    Photo.objects.filter(pk=photo.pk).update(event_type=junk)
    catalog_review.reject(admin_user, junk)
    photo.refresh_from_db()
    assert photo.event_type is None


def test_it_was_a_place(photo, alice, admin_user):
    proposal = propose(alice, photo, "Casa Greco")
    catalog_review.to_location(admin_user, proposal.event_type)
    proposal.refresh_from_db()
    photo.refresh_from_db()
    assert proposal.kind == Proposal.Kind.LOCATION and proposal.status == Status.ACCEPTED
    assert photo.location.name == "Casa Greco" and not photo.location.is_provisional


def test_new_places_go_to_the_same_review(photo, alice, admin_user):
    proposal = voting.create_proposal(alice, photo, Proposal.Kind.LOCATION, location_name="Ronda")
    assert proposal.in_review
    catalog_review.accept(admin_user, proposal.location, "Ronda (Málaga)")
    photo.refresh_from_db()
    assert photo.location.name == "Ronda (Málaga)"
    assert AuditLog.objects.filter(action="location.accept").exists()


def test_review_screens_and_permissions(client, photo, alice, admin_user):
    event = propose(alice, photo, "Graduación").event_type
    place = voting.create_proposal(alice, photo, Proposal.Kind.LOCATION, location_name="Ronda")
    client.force_login(alice)
    url = reverse("admin_event_type_review", args=[event.pk])
    assert client.post(url, {"action": "accept"}).status_code == 403

    client.force_login(admin_user)
    home = client.get(reverse("admin_home")).content.decode()
    assert home.count("1 pendiente de revisión") == 2
    content = client.get(reverse("admin_event_types")).content.decode()
    assert "Pendiente de revisión (1)" in content and "Es un lugar" in content
    content = client.get(reverse("admin_locations")).content.decode()
    assert "Pendiente de revisión (1)" in content and "Es un lugar" not in content

    client.post(url, {"action": "accept", "text": "Graduación"})
    client.post(reverse("admin_location_review", args=[place.location.pk]), {"action": "reject"})
    event.refresh_from_db()
    assert not event.is_provisional and not Location.objects.filter(name="Ronda").exists()


# --- Data migration -----------------------------------------------------------------------


def test_migration_puts_family_types_in_review(alice, boda):
    junk = EventType.objects.create(name="Viaje Mérida", created_by=alice)
    EventType.objects.update_or_create(name="Otro", defaults={"active": True})
    migration = importlib.import_module("catalog.migrations.0017_new_event_types_need_review")
    migration.to_review(apps, None)
    junk.refresh_from_db()
    boda.refresh_from_db()
    assert junk.is_provisional and not boda.is_provisional
    assert not EventType.objects.get(name="Otro").active


# --- Administrators are told ------------------------------------------------------------


def test_admins_get_a_review_digest(photo, alice, admin_user, settings, monkeypatch):
    from django.utils import timezone

    from notifications.models import NotificationSettings, PushSubscription
    from notifications.services import digest, push

    settings.VAPID_PUBLIC_KEY, settings.VAPID_PRIVATE_KEY = push.generate_vapid_keys()
    settings.NOTIFY_QUIET_HOURS = ""
    sent = []
    monkeypatch.setattr(push, "send", lambda sub, payload: sent.append(payload) or True)
    PushSubscription.objects.create(user=admin_user, endpoint="https://p/a", p256dh="k", auth="a")
    prefs = NotificationSettings.for_user(admin_user)
    prefs.new_photos = prefs.pending_votes = prefs.resolved = prefs.challenges = False
    prefs.reviews_sent_at = timezone.now() - timezone.timedelta(hours=2)
    prefs.save()

    propose(alice, photo, "Graduación")
    digest.send_digests()

    (payload,) = sent
    assert "para revisar" in payload["body"] and payload["url"] == reverse("admin_event_types")
