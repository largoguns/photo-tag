"""Push notifications: grouped digests, delivery, subscription views and profile card."""

import datetime
import json
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from pywebpush import WebPushException

from catalog.models import Photo, Proposal, Scan
from catalog.services import voting
from notifications.models import NotificationSettings, PushSubscription
from notifications.services import digest, push
from notifications.views import device_label

pytestmark = pytest.mark.django_db

# Wednesday 2026-11-11 12:00 and 23:30 local time (Europe/Madrid, UTC+1 in November).
NOON = datetime.datetime(2026, 11, 11, 11, 0, tzinfo=datetime.UTC)
NIGHT = datetime.datetime(2026, 11, 11, 22, 30, tzinfo=datetime.UTC)
LONG_AGO = NOON - datetime.timedelta(days=1)


@pytest.fixture(autouse=True)
def vapid(settings):
    settings.VAPID_PUBLIC_KEY, settings.VAPID_PRIVATE_KEY = push.generate_vapid_keys()
    settings.VAPID_SUBJECT = "mailto:test@example.com"
    settings.NOTIFY_MIN_INTERVAL_MINUTES = 60
    settings.NOTIFY_QUIET_HOURS = "22-9"


@pytest.fixture
def sent(monkeypatch):
    """Capture deliveries instead of calling the push services."""
    calls = []

    def fake_send(subscription, payload):
        calls.append((subscription, payload))
        return True

    monkeypatch.setattr(push, "send", fake_send)
    return calls


def subscribe(user, language="es", endpoint=None):
    return PushSubscription.objects.create(
        user=user,
        endpoint=endpoint or f"https://push.example.com/{user.username}-{language}",
        p256dh="key",
        auth="auth",
        language=language,
    )


def prefs_since(user, when=LONG_AGO, **flags):
    prefs = NotificationSettings.for_user(user)
    prefs.new_photos_sent_at = prefs.pending_votes_sent_at = prefs.resolved_sent_at = when
    for name, value in flags.items():
        setattr(prefs, name, value)
    prefs.save()
    return prefs


def make_photos(count, created_at=NOON):
    scan = Scan.objects.create(sha256=f"{count}".ljust(64, "c"), file_name="s.jpg")
    for index in range(count):
        photo = Photo.objects.create(
            scan=scan, file_name=f"p{index}.jpg", index=index, width=800, height=600
        )
        Photo.objects.filter(pk=photo.pk).update(created_at=created_at)


# --- Digest content ----------------------------------------------------------------


def test_new_photos_digest(alice, sent):
    subscribe(alice)
    prefs_since(alice)
    make_photos(3)

    assert digest.send_digests(NOON) == 1
    _, payload = sent[0]
    assert payload["body"] == "3 fotos nuevas para catalogar"
    assert payload["url"] == reverse("next_uncataloged")
    assert payload["tag"] == "phototag-new_photos"


def test_pending_votes_digest_excludes_own_and_voted(photo, alice, bob, carol, boda, sent):
    subscribe(alice)
    prefs_since(alice, new_photos=False)
    voting.create_proposal(bob, photo, Proposal.Kind.EVENT, event_type=boda)  # pending
    voting.create_proposal(
        alice, photo, Proposal.Kind.DATE, value={"precision": "year", "year": 1980}
    )
    voted = voting.create_proposal(
        carol, photo, Proposal.Kind.FACE_IDENTITY, face=photo.faces.first(), person_name="X"
    )
    voting.cast_vote(alice, voted, -1)
    Proposal.objects.update(created_at=NOON - datetime.timedelta(minutes=5))

    digest.send_digests(NOON)

    bodies = [payload["body"] for _, payload in sent]
    assert bodies == ["1 propuesta espera tu voto"]


def test_resolved_digest_counts_accepted_and_rejected(photo, alice, bob, carol, boda, sent):
    subscribe(alice)
    prefs_since(alice, new_photos=False, pending_votes=False)
    accepted = voting.create_proposal(alice, photo, Proposal.Kind.EVENT, event_type=boda)
    voting.cast_vote(bob, accepted, 1)
    rejected = voting.create_proposal(
        alice, photo, Proposal.Kind.FACE_IDENTITY, face=photo.faces.first(), person_name="Nadie"
    )
    voting.cast_vote(bob, rejected, -1)
    voting.cast_vote(carol, rejected, -1)
    Proposal.objects.update(decided_at=NOON - datetime.timedelta(minutes=1))

    digest.send_digests(NOON)

    ((_, payload),) = sent
    assert payload["body"] == "1 propuesta tuya aceptada · 1 propuesta tuya rechazada"
    assert payload["url"] == reverse("home")


def test_texts_never_include_person_names(photo, alice, bob, sent):
    subscribe(bob)
    prefs_since(bob)
    voting.create_proposal(
        alice,
        photo,
        Proposal.Kind.FACE_IDENTITY,
        face=photo.faces.first(),
        person_name="Abuela Carmen",
    )
    Proposal.objects.update(created_at=NOON)
    digest.send_digests(NOON + datetime.timedelta(minutes=1))
    assert sent
    assert all("Carmen" not in json.dumps(payload) for _, payload in sent)


def test_language_per_subscription(alice, sent):
    subscribe(alice, "es")
    subscribe(alice, "en")
    prefs_since(alice)
    make_photos(1)

    digest.send_digests(NOON)

    bodies = sorted(payload["body"] for _, payload in sent)
    assert bodies == ["1 foto nueva para catalogar", "1 new photo to catalog"]


# --- Grouping rules ----------------------------------------------------------------


def test_new_settings_do_not_notify_history(alice, sent):
    make_photos(5, created_at=LONG_AGO)
    subscribe(alice)
    NotificationSettings.for_user(alice)
    assert digest.send_digests(timezone.now()) == 0


def test_at_most_one_per_kind_and_interval(alice, sent):
    subscribe(alice)
    prefs_since(alice)
    make_photos(2)
    digest.send_digests(NOON)

    make_photos(4, created_at=NOON + datetime.timedelta(minutes=10))
    assert digest.send_digests(NOON + datetime.timedelta(minutes=30)) == 0  # within the hour

    assert digest.send_digests(NOON + datetime.timedelta(minutes=61)) == 1
    assert sent[-1][1]["body"] == "4 fotos nuevas para catalogar"


@pytest.mark.parametrize(
    ("utc_hour", "quiet"),
    [(20, False), (21, True), (23, True), (3, True), (7, True), (8, False)],
)
def test_quiet_hours_cross_midnight(utc_hour, quiet):
    # Europe/Madrid is UTC+1 in November: 21 UTC = 22 local, 7 UTC = 8 local.
    when = datetime.datetime(2026, 11, 11, utc_hour, 0, tzinfo=datetime.UTC)
    assert digest.in_quiet_hours(when, "22-9") is quiet


def test_quiet_hours_parsing():
    assert digest.parse_quiet_hours("22-9") == (22, 9)
    assert digest.parse_quiet_hours("") is None
    assert digest.parse_quiet_hours("9-9") is None
    assert digest.in_quiet_hours(NIGHT, "") is False


def test_nothing_is_sent_at_night_but_it_accumulates(alice, sent):
    subscribe(alice)
    prefs_since(alice)
    make_photos(2, created_at=NIGHT)
    assert digest.send_digests(NIGHT + datetime.timedelta(minutes=5)) == 0
    morning = datetime.datetime(2026, 11, 12, 9, 0, tzinfo=datetime.UTC)  # 10:00 local
    assert digest.send_digests(morning) == 1


def test_disabled_kinds_and_inactive_users_are_skipped(alice, bob, sent):
    subscribe(alice)
    prefs_since(alice, new_photos=False)
    subscribe(bob)
    prefs_since(bob)
    bob.must_change_password = True
    bob.save()
    make_photos(2)
    assert digest.send_digests(NOON) == 0


def test_users_without_devices_are_skipped(alice, sent):
    prefs_since(alice)
    make_photos(2)
    assert digest.send_digests(NOON) == 0


def test_failed_delivery_is_retried_next_pass(alice, monkeypatch):
    subscribe(alice)
    prefs = prefs_since(alice)
    make_photos(1)
    monkeypatch.setattr(push, "send", lambda subscription, payload: False)
    digest.send_digests(NOON)
    prefs.refresh_from_db()
    assert prefs.new_photos_sent_at == LONG_AGO


# --- Delivery ----------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code
        self.text = ""


def test_expired_subscription_is_deleted(alice, monkeypatch):
    subscription = subscribe(alice)

    def gone(**kwargs):
        raise WebPushException("gone", response=FakeResponse(410))

    monkeypatch.setattr(push, "webpush", gone)
    assert push.send(subscription, {"body": "x"}) is False
    assert not PushSubscription.objects.filter(pk=subscription.pk).exists()


def test_other_errors_keep_the_subscription(alice, monkeypatch):
    subscription = subscribe(alice)

    def failing(**kwargs):
        raise WebPushException("boom", response=FakeResponse(500))

    monkeypatch.setattr(push, "webpush", failing)
    assert push.send(subscription, {"body": "x"}) is False
    assert PushSubscription.objects.filter(pk=subscription.pk).exists()


def test_successful_send_uses_vapid(alice, monkeypatch, settings):
    subscription = subscribe(alice)
    captured = {}
    monkeypatch.setattr(push, "webpush", lambda **kwargs: captured.update(kwargs))

    assert push.send(subscription, {"body": "hola"}) is True
    assert captured["vapid_private_key"] == settings.VAPID_PRIVATE_KEY
    assert captured["vapid_claims"] == {"sub": "mailto:test@example.com"}
    assert json.loads(captured["data"]) == {"body": "hola"}
    subscription.refresh_from_db()
    assert subscription.last_success_at


def test_without_vapid_keys_nothing_is_sent(alice, settings, monkeypatch):
    settings.VAPID_PUBLIC_KEY = settings.VAPID_PRIVATE_KEY = ""
    monkeypatch.setattr(push, "webpush", lambda **kwargs: pytest.fail("must not send"))
    assert push.send(subscribe(alice), {}) is False
    assert digest.send_digests(NOON) == 0


def test_generated_keys_have_browser_format():
    public, private = push.generate_vapid_keys()
    assert len(public) == 87 and len(private) == 43
    assert "=" not in public + private


# --- Views and profile -------------------------------------------------------------


SUBSCRIPTION = {
    "endpoint": "https://fcm.googleapis.com/fcm/send/abc",
    "keys": {"p256dh": "BPk…", "auth": "xyz"},
}


def post_json(client, name, body, **extra):
    return client.post(reverse(name), json.dumps(body), content_type="application/json", **extra)


def test_subscription_views_require_login(client, db):
    for name in ("push_subscribe", "push_unsubscribe", "push_test"):
        assert post_json(client, name, {}).status_code == 302


def test_subscribe_and_unsubscribe(client, alice):
    client.force_login(alice)
    response = post_json(
        client,
        "push_subscribe",
        SUBSCRIPTION,
        HTTP_USER_AGENT="Mozilla/5.0 (Linux; Android 14) Chrome/130.0 Mobile Safari/537.36",
        HTTP_ACCEPT_LANGUAGE="en",
    )
    assert response.json()["device"] == "Chrome · Android"
    subscription = PushSubscription.objects.get()
    assert subscription.user == alice and subscription.language == "en"

    post_json(client, "push_unsubscribe", {"endpoint": SUBSCRIPTION["endpoint"]})
    assert not PushSubscription.objects.exists()


def test_cannot_unsubscribe_someone_else(client, alice, bob):
    subscribe(bob, endpoint=SUBSCRIPTION["endpoint"])
    client.force_login(alice)
    post_json(client, "push_unsubscribe", {"endpoint": SUBSCRIPTION["endpoint"]})
    assert PushSubscription.objects.filter(user=bob).exists()


@pytest.mark.parametrize(
    "body", [{}, {"endpoint": "http://insecure.example"}, {"endpoint": "https://x", "keys": {}}]
)
def test_invalid_subscriptions_are_rejected(client, alice, body):
    client.force_login(alice)
    assert post_json(client, "push_subscribe", body).status_code == 400


def test_test_notification(client, alice, sent):
    subscribe(alice)
    client.force_login(alice)
    response = post_json(client, "push_test", {})
    assert response.json()["ok"] is True
    assert sent[0][1]["tag"] == "phototag-test"


def test_profile_saves_preferences(client, alice):
    client.force_login(alice)
    client.post(reverse("profile"), {"save_notifications": "1", "new_photos": "on"})
    prefs = NotificationSettings.objects.get(user=alice)
    assert prefs.new_photos and not prefs.pending_votes and not prefs.resolved


def test_profile_card_only_with_vapid_keys(client, alice, settings):
    client.force_login(alice)
    content = client.get(reverse("profile")).content.decode()
    assert 'id="push-card"' in content
    assert f'data-vapid-key="{settings.VAPID_PUBLIC_KEY}"' in content

    settings.VAPID_PUBLIC_KEY = ""
    assert 'id="push-card"' not in client.get(reverse("profile")).content.decode()


def test_device_labels():
    assert (
        device_label("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Version/17.0 Mobile Safari/604.1")
        == "Safari · iOS"
    )
    assert (
        device_label("Mozilla/5.0 (Android 14; Mobile; rv:130.0) Gecko Firefox/130.0")
        == "Firefox · Android"
    )
    assert device_label("") == ""


# --- Service worker and deployment -----------------------------------------------------


def test_service_worker_handles_push_and_clicks(client):
    body = client.get("/sw.js").content.decode()
    assert 'addEventListener("push"' in body
    assert 'addEventListener("notificationclick"' in body
    assert "badge" in body and "96.png" in body  # escapejs encodes the hyphen


@pytest.mark.skipif(
    not (Path(settings.BASE_DIR) / "docker-compose.yml").exists(),
    reason="compose file is not in the image",
)
def test_worker_has_outbound_network_and_no_ports():
    compose = (Path(settings.BASE_DIR) / "docker-compose.yml").read_text()
    assert "networks: [internal, egress]" in compose
    assert "egress: {}" in compose
    assert "\n    ports:" not in compose
