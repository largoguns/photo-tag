"""Family quiz (spec section 5.14)."""

import datetime
import random

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from catalog.models import Face, Person, PersonNickname, Photo, Scan
from catalog.services import relationships
from catalog.services.names import name_key
from gamification.models import Achievement, QuizAttempt, ScoreEvent
from gamification.services import quiz, scoring

pytestmark = pytest.mark.django_db
Kind = QuizAttempt.Kind

NAMES = [
    "Ana", "Luis", "Carmen", "Pepe", "Lola", "Paco",
    "Rosa", "Juan", "Marta", "Pedro", "Elena", "Jorge",
]  # fmt: skip


@pytest.fixture
def album(alice):
    """12 identified people (one face each, photos from 1960 to 1993, the last one dated
    by decade), a nickname for 10 of them and a three-generation family."""
    scan = Scan.objects.create(sha256="q" * 64, file_name="quiz.jpg")
    people = []
    for index, name in enumerate(NAMES):
        precision = Photo.DatePrecision.DECADE if index == 11 else Photo.DatePrecision.YEAR
        photo = Photo.objects.create(
            scan=scan,
            file_name=f"quiz_{index:02}.jpg",
            index=index,
            width=800,
            height=600,
            date_precision=precision,
            date_value=datetime.date(1960 + 3 * index, 1, 1),
        )
        person = Person.objects.create(display_name=name)
        Face.objects.create(photo=photo, x=10, y=10, w=50, h=50, person=person)
        if index < 10:
            text = f"Mote {index}"
            PersonNickname.objects.create(person=person, text=text, text_key=name_key(text))
        people.append(person)
    ana, luis, carmen, pepe, lola, paco, rosa, juan, *_rest = people
    relationships.add(alice, luis, "partner", other=ana)
    for child in (carmen, pepe, lola):
        relationships.add(alice, child, "parent", other=ana)
        relationships.add(alice, child, "parent", other=luis)
    relationships.add(alice, carmen, "partner", other=paco)
    for child in (rosa, juan):
        relationships.add(alice, child, "parent", other=carmen)
        relationships.add(alice, child, "parent", other=paco)
    return people


def play(user, kind, right=True, rng=None):
    attempt = quiz.start(user, kind, rng=rng or random.Random(1))
    for question in attempt.questions:
        wrong = (question["answer"] + 1) % len(question["options"])
        attempt, *_ = quiz.answer(attempt, question["answer"] if right else wrong)
    return attempt


# --- Availability and questions ----------------------------------------------------------


def test_nothing_is_available_without_data(alice):
    for kind in Kind.values:
        info = quiz.availability(kind)
        assert not info.ready and "0 de 10" in str(info.missing)
        with pytest.raises(ValidationError):
            quiz.start(alice, kind)


@pytest.mark.parametrize("kind", Kind.values)
def test_ten_questions_with_four_different_options(album, alice, kind):
    assert quiz.availability(kind).ready
    attempt = quiz.start(alice, kind, rng=random.Random(7))
    assert len(attempt.questions) == 10
    for question in attempt.questions:
        labels = [option["label"] for option in question["options"]]
        assert len(labels) == 4 and len(set(labels)) == 4
        assert 0 <= question["answer"] < 4


def test_who_is_it_does_not_give_the_answer_away(album, alice):
    attempt = quiz.start(alice, Kind.FACES, rng=random.Random(3))
    for question in attempt.questions:
        assert question["images"][0].startswith("/faces/")  # only the face id
        assert all(option["avatar"] == "" for option in question["options"])


def test_date_options_are_never_in_the_future(album, alice, monkeypatch):
    attempt = quiz.start(alice, Kind.DATES, rng=random.Random(5))
    this_year = timezone.localdate().year
    for question in attempt.questions:
        for option in question["options"]:
            digits = "".join(c for c in option["label"] if c.isdigit())
            assert int(digits) <= this_year
    assert any("Década de" in q["options"][q["answer"]]["label"] for q in attempt.questions)


def test_namesakes_never_appear_as_two_options(album, alice):
    for person in album[6:]:
        Person.objects.filter(pk=person.pk).update(display_name="Ana")
    for seed in range(5):
        attempt = quiz.start(alice, Kind.FACES, rng=random.Random(seed))
        for question in attempt.questions:
            labels = [option["label"] for option in question["options"]]
            assert len(set(labels)) == len(labels)


def test_kinship_questions_come_from_the_tree(album, alice):
    attempt = quiz.start(alice, Kind.KINSHIP, rng=random.Random(2))
    texts = " ".join(q["text"] for q in attempt.questions)
    assert texts.count("¿Qué es") == 10
    answers = {q["options"][q["answer"]]["label"] for q in attempt.questions}
    assert answers & {
        "Hermano",
        "Abuelo",
        "Nieto",
        "Tío",
        "Sobrino",
        "Pareja",
        "Progenitor",
        "Hijo",
    }


# --- Playing and points -------------------------------------------------------------------


def test_points_per_right_answer_and_bonus_for_a_perfect_quiz(album, alice):
    attempt = play(alice, Kind.FACES)
    assert attempt.is_finished and attempt.correct == 10 and attempt.scored
    assert scoring.total(alice) == 10 + quiz.PERFECT_BONUS
    assert Achievement.objects.filter(user=alice, code="quiz_perfect").exists()

    play(alice, Kind.FACES, right=False)
    assert scoring.total(alice) == 10 + quiz.PERFECT_BONUS  # 0/10 adds nothing


def test_only_three_quizzes_of_a_kind_score_each_day(album, alice):
    scored = [play(alice, Kind.DATES).scored for _ in range(4)]
    assert scored == [True, True, True, False]
    assert quiz.scored_left(alice, Kind.DATES) == 0
    assert quiz.scored_left(alice, Kind.FACES) == 3  # per kind
    assert scoring.total(alice) == 3 * (10 + quiz.PERFECT_BONUS)


def test_administrators_play_without_points(album, admin_user):
    attempt = play(admin_user, Kind.NICKNAMES)
    assert attempt.correct == 10 and not attempt.scored
    assert not ScoreEvent.objects.exists()


def test_a_finished_quiz_cannot_be_answered_again(album, alice):
    attempt = play(alice, Kind.FACES)
    with pytest.raises(ValidationError):
        quiz.answer(attempt, 0)


def test_three_perfect_quizzes_of_a_kind_unlock_its_badge(album, alice):
    for _ in range(3):
        play(alice, Kind.KINSHIP)
    codes = set(Achievement.objects.filter(user=alice).values_list("code", flat=True))
    assert {"quiz_perfect", "quiz_kinship"} <= codes


def test_rebuild_keeps_quiz_points(album, alice):
    play(alice, Kind.NICKNAMES)
    before = scoring.total(alice)
    scoring.rebuild()
    assert scoring.total(alice) == before
    assert ScoreEvent.objects.filter(user=alice, reason="quiz_perfect_nicknames").exists()


# --- Screens ------------------------------------------------------------------------------


def test_play_through_the_screens(client, album, alice):
    client.force_login(alice)
    content = client.get(reverse("quiz_home")).content.decode()
    assert "¿Quién es?" in content and "Hoy te quedan 3 con puntos" in content

    response = client.post(reverse("quiz_start"), {"kind": Kind.FACES})
    attempt = QuizAttempt.objects.get()
    assert response.url == reverse("quiz_play", args=[attempt.pk])

    for number, question in enumerate(attempt.questions, start=1):
        page = client.get(response.url).content.decode()
        assert f"Pregunta {number} de 10" in page
        assert '"answer"' not in page and "right" not in page.split('id="quiz-card"')[1]
        answered = client.post(
            reverse("quiz_answer", args=[attempt.pk]),
            {"option": question["answer"]},
            HTTP_HX_REQUEST="true",
        ).content.decode()
        assert "¡Correcto!" in answered and "quiz-option right" in answered

    result = client.get(response.url).content.decode()
    assert "10 de 10" in result and "+20 puntos" in result and "Pleno" in result


def test_quiz_of_another_user_is_not_found(client, album, alice, bob):
    attempt = quiz.start(alice, Kind.FACES, rng=random.Random(1))
    client.force_login(bob)
    assert client.get(reverse("quiz_play", args=[attempt.pk])).status_code == 404
    assert client.post(reverse("quiz_answer", args=[attempt.pk]), {"option": 0}).status_code == 404


def test_unavailable_kinds_say_what_is_missing(client, alice):
    client.force_login(alice)
    content = client.get(reverse("quiz_home")).content.decode()
    assert "Faltan apodos: hay 0 de 10." in content
    response = client.post(reverse("quiz_start"), {"kind": Kind.NICKNAMES}, follow=True)
    assert "Faltan apodos" in response.content.decode()


def test_home_and_ranking_link_to_the_quiz(client, alice):
    client.force_login(alice)
    url = reverse("quiz_home")
    assert url in client.get(reverse("home")).content.decode()
    assert url in client.get(reverse("ranking")).content.decode()
