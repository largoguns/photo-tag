"""Questions between users while cataloging (spec section 5.17)."""

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone, translation

from accounts.models import AuditLog
from catalog.models import Face, Photo, Question
from catalog.services import questions

pytestmark = pytest.mark.django_db
Topic = Question.Topic


@pytest.fixture(autouse=True)
def spanish():
    with translation.override("es"):
        yield


# --- Asking, answering, resolving ---------------------------------------------------------


def test_ask_about_a_face_to_someone(photo, alice, bob, carol):
    face = photo.faces.order_by("x").first()
    question = questions.ask(alice, photo, Topic.FACE, "¿Quién es?", face=face, recipients=[bob])
    assert question.is_open and question.face == face
    assert list(question.recipients.all()) == [bob]
    assert question.topic_label() == "Cara 1"
    assert AuditLog.objects.filter(action="question.create").exists()


def test_a_face_of_another_photo_or_empty_text_is_refused(photo, alice):
    other = Photo.objects.create(scan=photo.scan, file_name="o.jpg", index=1, width=10, height=10)
    stranger = Face.objects.create(photo=other, x=1, y=1, w=5, h=5)
    with pytest.raises(ValidationError):
        questions.ask(alice, photo, Topic.FACE, "¿Quién?", face=stranger)
    with pytest.raises(ValidationError):
        questions.ask(alice, photo, Topic.GENERAL, "   ")
    with pytest.raises(ValidationError):
        questions.ask(alice, photo, "weather", "¿?")


def test_answer_resolve_and_reopen(photo, alice, bob, carol, admin_user):
    question = questions.ask(alice, photo, Topic.DATE, "¿De qué año es?")
    questions.answer(bob, question, "Del 85, seguro")
    with pytest.raises(PermissionDenied):
        questions.resolve(carol, question)  # only the author or an administrator
    questions.resolve(alice, question)
    question.refresh_from_db()
    assert not question.is_open and question.resolved_by == alice
    # Something new reopens it.
    questions.answer(carol, question, "Yo diría del 86")
    question.refresh_from_db()
    assert question.is_open and question.resolved_by is None
    questions.resolve(admin_user, question)
    questions.reopen(admin_user, question)
    question.refresh_from_db()
    assert question.is_open


# --- Who it is for --------------------------------------------------------------------------


def test_boxes_and_pending_count(photo, alice, bob, carol):
    to_bob = questions.ask(alice, photo, Topic.EVENT, "¿Qué boda es?", recipients=[bob])
    to_all = questions.ask(alice, photo, Topic.LOCATION, "¿Dónde es?")
    assert set(questions.inbox(bob, "for_me")) == {to_bob, to_all}
    assert set(questions.inbox(carol, "for_me")) == {to_all}  # not addressed to carol
    assert list(questions.inbox(alice, "for_me")) == []  # her own
    assert set(questions.inbox(alice, "mine")) == {to_bob, to_all}
    # Answering a question for everyone takes it out of «para mí».
    questions.answer(carol, to_all, "En Sevilla")
    assert questions.pending_count(carol) == 0
    assert questions.pending_count(bob) == 2
    questions.resolve(alice, to_bob)
    assert questions.pending_count(bob) == 1
    assert set(questions.inbox(bob, "all", status=Question.Status.RESOLVED)) == {to_bob}


# --- Screens --------------------------------------------------------------------------------


def test_photo_panel_ask_answer_and_resolve(client, photo, alice, bob):
    face = photo.faces.order_by("x").first()
    client.force_login(alice)
    page = client.get(reverse("photo_detail", args=[photo.pk]), {"ask_face": face.pk})
    content = page.content.decode()
    assert 'id="questions-panel" open' in content  # opened to ask about that face
    assert f'value="face:{face.pk}" selected' in content

    client.post(
        reverse("ask_question", args=[photo.pk]),
        {"topic": f"face:{face.pk}", "text": "¿Es el tito Paco?", "recipients": [bob.pk]},
    )
    question = Question.objects.get()
    assert question.face == face and list(question.recipients.all()) == [bob]

    client.force_login(bob)
    content = client.get(reverse("photo_detail", args=[photo.pk])).content.decode()
    assert "¿Es el tito Paco?" in content and "1 abierta" in content
    assert "Marcar resuelta" not in content  # bob is not the author
    client.post(reverse("answer_question", args=[question.pk]), {"text": "Sí, es él"})
    client.force_login(alice)
    client.post(reverse("resolve_question", args=[question.pk]))
    question.refresh_from_db()
    assert not question.is_open and question.answers.get().text == "Sí, es él"


def test_inbox_menu_and_home(client, photo, alice, bob):
    questions.ask(alice, photo, Topic.GENERAL, "¿Alguien sabe algo de esta foto?", recipients=[bob])
    client.force_login(bob)
    home = client.get(reverse("home")).content.decode()
    assert "1 pregunta para ti" in home and 'class="avatar-dot"' in home
    assert '<span class="menu-count">1</span>' in home
    inbox = client.get(reverse("questions")).content.decode()
    assert "¿Alguien sabe algo de esta foto?" in inbox
    assert client.get(reverse("questions"), {"box": "mine"}).context["items"].count() == 0


def test_gallery_marks_and_filters_photos_with_open_questions(client, photo, alice):
    other = Photo.objects.create(scan=photo.scan, file_name="o.jpg", index=1, width=10, height=10)
    questions.ask(alice, photo, Topic.GENERAL, "¿?")
    client.force_login(alice)
    response = client.get(reverse("gallery"), {"status": "questions"})
    assert response.context["photo_count"] == 1
    content = response.content.decode()
    assert "Con preguntas abiertas" in content and "jitem-question" in content
    assert other.pk not in [p.pk for block in response.context["blocks"] for p in block.photos]


# --- Push -----------------------------------------------------------------------------------


def test_push_digest_for_questions_and_answers(photo, alice, bob, settings, monkeypatch):
    from notifications.models import NotificationSettings, PushSubscription
    from notifications.services import digest, push

    settings.VAPID_PUBLIC_KEY, settings.VAPID_PRIVATE_KEY = push.generate_vapid_keys()
    settings.NOTIFY_QUIET_HOURS = ""
    sent = []
    monkeypatch.setattr(push, "send", lambda sub, payload: sent.append(payload) or True)
    past = timezone.now() - timezone.timedelta(hours=2)
    for user in (alice, bob):
        PushSubscription.objects.create(
            user=user, endpoint=f"https://p/{user.pk}", p256dh="k", auth="a"
        )
        prefs = NotificationSettings.for_user(user)
        prefs.new_photos = prefs.pending_votes = prefs.resolved = prefs.challenges = False
        prefs.questions_sent_at = prefs.answers_sent_at = past
        prefs.save()

    question = questions.ask(alice, photo, Topic.GENERAL, "¿De qué año?", recipients=[bob])
    questions.answer(bob, question, "1985")
    digest.send_digests()

    bodies = {payload["url"]: payload["body"] for payload in sent}
    assert "1 pregunta nueva para ti" in bodies[reverse("questions")]
    assert "1 respuesta nueva" in bodies[reverse("questions") + "?box=mine"]
