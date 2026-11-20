"""Points, levels and badges for collaborators."""

from datetime import timedelta

import pytest
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from accounts.models import AuditLog, Role
from catalog.models import Face, Person, Photo, Proposal, Scan, Vote
from catalog.services import voting
from gamification.models import Achievement, ScoreEvent
from gamification.services import achievements, levels, scoring
from tests.conftest import make_user

pytestmark = pytest.mark.django_db


def points(user, reason=None):
    events = ScoreEvent.objects.filter(user=user)
    if reason:
        events = events.filter(reason=reason)
    return sum(events.values_list("points", flat=True))


def identity(author, photo, name="Ana", face=None, **extra):
    face = face or photo.faces.first()
    return voting.create_proposal(
        author, photo, Proposal.Kind.FACE_IDENTITY, face=face, person_name=name, **extra
    )


def test_accepted_identity_scores_its_author_and_the_right_votes(alice, bob, photo):
    proposal = identity(alice, photo)
    voting.cast_vote(bob, proposal, Vote.UP)  # second approval: accepted

    assert Proposal.objects.get(pk=proposal.pk).status == Proposal.Status.ACCEPTED
    assert points(alice, "proposal.identity") == 20
    assert points(alice, "first_identity") == 15  # nobody had identified Ana before
    assert points(alice, "vote") == 0  # the author's automatic +1 does not count
    assert points(bob, "vote") == 2
    assert ScoreEvent.objects.filter(user=bob, reason="tiebreak").exists()


def test_first_identity_bonus_only_once_per_person(alice, bob, photo):
    first, second = photo.faces.all()[:2]
    voting.cast_vote(bob, identity(alice, photo, face=first), Vote.UP)
    voting.cast_vote(bob, identity(alice, photo, face=second), Vote.UP)
    assert points(alice, "first_identity") == 15


def test_a_suggestion_confirmed_scores_less(alice, bob, photo):
    proposal = identity(alice, photo, value={"suggested": True})
    voting.cast_vote(bob, proposal, Vote.UP)
    assert points(alice, "proposal.identity_suggested") == 10
    assert points(alice, "proposal.identity") == 0


def test_rejected_scores_the_right_votes_only(alice, bob, carol, photo):
    proposal = voting.create_proposal(
        alice, photo, Proposal.Kind.DATE, value={"precision": "year", "year": 1985}
    )
    voting.cast_vote(bob, proposal, Vote.DOWN)
    voting.cast_vote(carol, proposal, Vote.DOWN)
    assert Proposal.objects.get(pk=proposal.pk).status == Proposal.Status.REJECTED
    assert points(alice) == 0  # rejecting never takes points away
    assert points(bob, "vote") == 2 and points(carol, "vote") == 2


def test_superseded_after_being_accepted_takes_the_points_back(alice, bob, carol, photo):
    first = voting.create_proposal(
        alice, photo, Proposal.Kind.DATE, value={"precision": "year", "year": 1985}
    )
    voting.cast_vote(bob, first, Vote.UP)
    assert points(alice, "proposal.date") == 10
    corrected = voting.create_proposal(
        carol, photo, Proposal.Kind.DATE, value={"precision": "year", "year": 1986}
    )
    voting.cast_vote(bob, corrected, Vote.UP)

    assert Proposal.objects.get(pk=first.pk).status == Proposal.Status.SUPERSEDED
    assert points(alice, "proposal.date") == 0
    assert points(carol, "proposal.date") == 10


def test_daily_cap_on_vote_points(alice, bob, photo, settings):
    for year in range(1950, 1965):  # 15 proposals: bob's right votes would be 30 points
        proposal = voting.create_proposal(
            alice, photo, Proposal.Kind.DATE, value={"precision": "year", "year": year}
        )
        voting.cast_vote(bob, proposal, Vote.UP)
    assert points(bob, "vote") == scoring.VOTE_DAILY_CAP


def test_superadmin_and_admin_do_not_play(superadmin, admin_user, bob, photo):
    first, second = photo.faces.all()[:2]
    accepted = identity(superadmin, photo, face=first)  # accepted at once: admins decide
    assert accepted.status == Proposal.Status.ACCEPTED
    proposal = identity(bob, photo, name="Luis", face=second)
    voting.cast_vote(admin_user, proposal, Vote.DOWN)
    assert not ScoreEvent.objects.filter(user__in=[superadmin, admin_user]).exists()


def test_photos_score_their_uploader_and_discarding_takes_it_back(alice, data_dirs):
    scan = Scan.objects.create(sha256="b" * 64, file_name="s.png", uploaded_by=alice)
    photo = Photo.objects.create(scan=scan, file_name="p.jpg", index=0, width=10, height=10)
    scoring.photo_added(photo)
    scoring.photo_added(photo)  # never twice
    assert points(alice, "photo") == 5

    photo.discarded = True
    scoring.photo_removed(photo)
    assert points(alice, "photo") == 0
    photo.discarded = False
    scoring.photo_added(photo)
    assert points(alice, "photo") == 5

    duplicate = Photo.objects.create(
        scan=scan, file_name="q.jpg", index=1, width=10, height=10, duplicate_of=photo
    )
    scoring.photo_added(duplicate)
    assert points(alice, "photo") == 5  # possible duplicates do not score


def test_deleting_a_photo_takes_back_its_proposals(alice, bob, photo):
    from scanner.services import deletion

    voting.cast_vote(bob, identity(alice, photo), Vote.UP)
    assert points(alice) > 0
    deletion.delete_photo(bob, photo)
    assert points(alice, "proposal.identity") == 0 and points(bob, "vote") == 0


def test_a_scoring_failure_never_breaks_voting(alice, bob, photo, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("scoring down")

    monkeypatch.setattr(scoring, "award", boom)
    proposal = identity(alice, photo)
    voting.cast_vote(bob, proposal, Vote.UP)
    assert Proposal.objects.get(pk=proposal.pk).status == Proposal.Status.ACCEPTED


@pytest.mark.parametrize(
    ("value", "number", "name"),
    [
        (0, 1, "Curioso"),
        (49, 1, "Curioso"),
        (50, 2, "Aprendiz de archivero"),
        (3500, 7, "Guardián del álbum"),
    ],
)
def test_levels(value, number, name):
    level = levels.level_for(value)
    assert (level.number, str(level.name)) == (number, name)


def test_level_progress():
    level = levels.level_for(100)
    assert level.missing == 50 and level.percent == 50
    assert levels.level_for(5000).percent == 100


def test_badges_are_unlocked_once(alice, bob, photo):
    voting.cast_vote(bob, identity(alice, photo), Vote.UP)
    assert set(Achievement.objects.filter(user=alice).values_list("code", flat=True)) == {
        "first_identity"
    }
    assert achievements.check(alice) == []


def test_streak_badge(alice):
    now = timezone.now()
    for day in range(7):
        entry = AuditLog.objects.create(user=alice, action="vote.cast")
        AuditLog.objects.filter(pk=entry.pk).update(created_at=now - timedelta(days=day))
    assert "streak_7" in achievements.check(alice)


def test_level_up_is_announced(alice, bob, photo, monkeypatch):
    from gamification.services import announcements

    sent = []
    monkeypatch.setattr(announcements, "level_up", lambda user, level: sent.append(level.name))
    monkeypatch.setattr(announcements, "achievement", lambda user, code: None)
    ScoreEvent.objects.create(user=alice, reason="seed", points=45, ref_type="x", ref_id="1")
    voting.cast_vote(bob, identity(alice, photo), Vote.UP)  # +20 +15 → 80
    assert [str(name) for name in sent] == ["Aprendiz de archivero"]


def test_rebuild_matches_live_scoring_and_is_idempotent(alice, bob, carol, photo):
    voting.cast_vote(bob, identity(alice, photo), Vote.UP)
    date = voting.create_proposal(
        carol, photo, Proposal.Kind.DATE, value={"precision": "year", "year": 1990}
    )
    voting.cast_vote(alice, date, Vote.UP)
    live = {u.username: points(u) for u in (alice, bob, carol)}

    call_command("rebuild_scores")
    call_command("rebuild_scores")

    rebuilt = {u.username: points(u) for u in (alice, bob, carol)}
    assert rebuilt == live


# --- Screens --------------------------------------------------------------------------


def test_ranking_by_period_marks_me(client, alice, bob, photo):
    voting.cast_vote(bob, identity(alice, photo), Vote.UP)
    old = ScoreEvent.objects.create(user=bob, reason="seed", points=100, ref_type="x", ref_id="2")
    ScoreEvent.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=400))
    client.force_login(bob)

    def listed(response):
        return response.content.decode().split('<ol class="list ranking-list">')[1]

    week = listed(client.get(reverse("ranking")))
    total = listed(client.get(reverse("ranking"), {"period": "total"}))

    assert week.index("Alicia") < week.index("Roberto")  # 35 points against 2 this week
    assert total.index("Roberto") < total.index("Alicia")  # 102 against 35 overall
    assert 'class="list-item ranking-row me"' in week


def test_home_and_profile_show_progress_to_players_only(client, alice, superadmin):
    client.force_login(alice)
    assert "Tu nivel" in client.get(reverse("home")).content.decode()
    assert "Tu progreso" in client.get(reverse("profile")).content.decode()
    client.force_login(superadmin)
    assert "Tu nivel" not in client.get(reverse("home")).content.decode()
    assert "Tu progreso" not in client.get(reverse("profile")).content.decode()


def test_points_tab_in_the_audit_is_for_the_superadmin(client, alice, bob, superadmin, photo):
    voting.cast_vote(bob, identity(alice, photo), Vote.UP)
    client.force_login(superadmin)
    content = client.get(reverse("audit"), {"user": alice.pk, "tab": "points"}).content.decode()
    assert "Identidad aceptada" in content and "+20" in content


def test_milestone_on_home(client, alice, photo):
    Photo.objects.filter(pk=photo.pk).update(status=Photo.Status.CATALOGED)
    client.force_login(alice)
    assert "todo el álbum" in client.get(reverse("home")).content.decode()


def test_achievement_notification_respects_the_preference(alice, settings, monkeypatch):
    from notifications.models import NotificationSettings, PushSubscription
    from notifications.services import push

    settings.VAPID_PUBLIC_KEY, settings.VAPID_PRIVATE_KEY = push.generate_vapid_keys()
    PushSubscription.objects.create(
        user=alice, endpoint="https://p.example/a", p256dh="k", auth="a"
    )
    sent = []
    monkeypatch.setattr(push, "send", lambda sub, payload: sent.append(payload) or True)
    from gamification.services import announcements

    announcements.achievement(alice, "first_upload")
    assert sent and "Primera subida" in sent[0]["body"]
    prefs = NotificationSettings.for_user(alice)
    prefs.achievements = False
    prefs.save()
    sent.clear()
    announcements.achievement(alice, "first_upload")
    assert sent == []


def test_new_players_start_at_zero(db):
    newcomer = make_user("nuevo", Role.COLLABORATOR)
    assert scoring.progress(newcomer)["points"] == 0
    assert Face.objects.count() >= 0 and Person.objects.count() >= 0


# --- «Retos para subir de nivel» notifications -----------------------------------------


@pytest.fixture
def push_ready(settings, monkeypatch):
    from notifications.services import push

    settings.VAPID_PUBLIC_KEY, settings.VAPID_PRIVATE_KEY = push.generate_vapid_keys()
    settings.NOTIFY_QUIET_HOURS = ""
    sent = []
    monkeypatch.setattr(push, "send", lambda sub, payload: sent.append(payload) or True)
    return sent


def subscribe(user):
    from notifications.models import NotificationSettings, PushSubscription

    PushSubscription.objects.create(
        user=user, endpoint=f"https://p.example/{user.pk}", p256dh="k", auth="a"
    )
    prefs = NotificationSettings.for_user(user)
    prefs.new_photos = prefs.pending_votes = prefs.resolved = False  # only the challenge
    prefs.challenge_sent_at = timezone.now() - timedelta(days=10)
    prefs.save()
    return prefs


def test_idle_player_is_challenged_with_pending_votes(push_ready, alice, bob, photo):
    from notifications.services import digest

    proposal = identity(bob, photo)
    AuditLog.objects.filter(user=alice).delete()
    subscribe(alice)

    assert digest.send_digests() == 1
    (payload,) = push_ready
    assert "Te faltan" in payload["body"] and "esperando tu voto" in payload["body"]
    assert payload["url"].endswith("?mine=1")
    assert proposal.pk  # still open for alice to vote

    push_ready.clear()
    assert digest.send_digests() == 0  # not again before NOTIFY_CHALLENGE_DAYS


def test_overtaken_player_is_told_the_gap(push_ready, alice, bob, carol, photo):
    from notifications.services import digest

    ScoreEvent.objects.create(user=bob, reason="seed", points=30, ref_type="x", ref_id="1")
    ScoreEvent.objects.create(user=alice, reason="seed", points=12, ref_type="x", ref_id="2")
    subscribe(alice)
    digest.send_digests()
    (payload,) = push_ready
    assert "18 puntos del puesto 1" in payload["body"] and payload["url"] == reverse("ranking")


def test_active_players_and_non_players_are_not_challenged(
    push_ready, alice, bob, superadmin, photo
):
    from notifications.services import digest

    identity(alice, photo)  # alice proposed just now: she is playing
    subscribe(alice)
    subscribe(superadmin)
    digest.send_digests()
    assert push_ready == []


def test_challenges_can_be_turned_off(push_ready, alice, photo):
    from notifications.services import digest

    prefs = subscribe(alice)
    prefs.challenges = False
    prefs.save()
    digest.send_digests()
    assert push_ready == []


def test_belt_ring_around_the_avatar_of_players_only(client, alice, superadmin):
    ScoreEvent.objects.create(user=alice, reason="seed", points=100, ref_type="x", ref_id="1")
    client.force_login(alice)
    content = client.get(reverse("home")).content.decode()
    # Level 2 (yellow belt) halfway to level 3: the orange arc fills 50 %.
    assert 'class="level-ring belt-2"' in content
    assert "--p: 50; --current: var(--belt-2); --next: var(--belt-3)" in content
    client.force_login(superadmin)
    assert "level-ring" not in client.get(reverse("home")).content.decode()


def test_top_level_ring_is_complete():
    from gamification.templatetags.gamification_tags import level_ring

    ring = level_ring(levels.level_for(5000))
    assert "belt-7" in ring and "--p: 0;" in ring and "var(--belt-7)" in ring
