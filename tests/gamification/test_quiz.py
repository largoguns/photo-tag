"""Family quiz (spec section 5.14)."""

import datetime
import random

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from catalog.models import Face, Location, Person, PersonNickname, Photo, Scan
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


EMOJIS = ["🐦", "🍷", "🎸", "⚽", "🌹", "🚲", "🐟", "☕", "🎨", "📚"]
PLACES = [
    ("Casa de la abuela", "Ronda", 36.74, -5.16),
    ("Plaza de toros", "Ronda", 36.74, -5.17),
    ("Playa de la Malagueta", "Málaga", 36.72, -4.41),
    ("Sierra Nevada", "Granada", 37.09, -3.39),
    ("Puerto de Cádiz", "Cádiz", 36.53, -6.29),
]


def place(name, locality="", latitude=None, longitude=None, **extra):
    return Location.objects.create(
        name=name,
        name_key=name_key(name),
        locality=locality,
        latitude=latitude,
        longitude=longitude,
        **extra,
    )


@pytest.fixture
def album(alice):
    """12 identified people (one face each, photos from 1960 to 1993, the last one dated
    by decade, taken in 5 places), a nickname with an emoji for 10 of them and a
    three-generation family."""
    scan = Scan.objects.create(sha256="q" * 64, file_name="quiz.jpg")
    places = [place(*spec) for spec in PLACES]
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
            location=places[index % len(places)],
        )
        person = Person.objects.create(display_name=name)
        Face.objects.create(photo=photo, x=10, y=10, w=50, h=50, person=person)
        if index < 10:
            text = f"Mote {index}"
            PersonNickname.objects.create(
                person=person, text=text, text_key=name_key(text), emoji=EMOJIS[index]
            )
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
        answer = question["answer"]
        if isinstance(answer, list):
            wrong = [(answer[0] + 1) % len(question["options"]), *answer[1:]]
        else:
            wrong = (answer + 1) % len(question["options"])
        attempt, *_ = quiz.answer(attempt, answer if right else wrong)
    return attempt


# --- Availability and questions ----------------------------------------------------------


def test_nothing_is_available_without_data(alice):
    for kind in Kind.values:
        info = quiz.availability(kind)
        assert not info.ready and "0 de 10" in str(info.missing)
        with pytest.raises(ValidationError):
            quiz.start(alice, kind)


@pytest.mark.parametrize("kind", [kind for kind in Kind.values if kind != Kind.EMOJIS])
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


# --- Quiz images and timing (anti-cheat) --------------------------------------------------


@pytest.fixture
def photo_files(album, data_dirs):
    """Real files for the album's photos, so the quiz image view can render them."""
    from PIL import Image

    data_dirs.joinpath("photos").mkdir(exist_ok=True)
    for photo in Photo.objects.all():
        Image.new("RGB", (800, 600), (90, 120, 150)).save(data_dirs / "photos" / photo.file_name)
    return album


def test_photo_questions_keep_the_photo_id_private(album, alice):
    attempt = quiz.start(alice, Kind.DATES, rng=random.Random(4))
    for question in attempt.questions:
        assert question["photo_id"] and not question["images"]
        assert set(quiz.public_question(question)) == {"text", "images", "options"}


def test_the_page_never_shows_the_photo_id(client, photo_files, alice):
    client.force_login(alice)
    attempt = quiz.start(alice, Kind.DATES, rng=random.Random(4))
    page = client.get(reverse("quiz_play", args=[attempt.pk])).content.decode()
    image_url = reverse("quiz_image", args=[attempt.pk, 0, 5])
    assert image_url in page
    assert "/photos/" not in page
    response = client.get(image_url)
    assert response.status_code == 200 and response["Content-Type"] == "image/jpeg"


def test_quiz_images_only_for_the_owner_and_reached_questions(client, photo_files, alice, bob):
    attempt = quiz.start(alice, Kind.DATES, rng=random.Random(4))
    client.force_login(alice)
    client.get(reverse("quiz_play", args=[attempt.pk]))
    assert client.get(reverse("quiz_image", args=[attempt.pk, 1, 5])).status_code == 404
    assert client.get(reverse("quiz_image", args=[attempt.pk, 0, 6])).status_code == 404
    quiz.answer(attempt, attempt.questions[0]["answer"])
    assert client.get(reverse("quiz_image", args=[attempt.pk, 0, 5])).status_code == 200
    assert client.get(reverse("quiz_image", args=[attempt.pk, 1, 5])).status_code == 200
    client.force_login(bob)
    assert client.get(reverse("quiz_image", args=[attempt.pk, 0, 5])).status_code == 404


def test_the_first_time_a_question_is_shown_is_kept(album, alice, monkeypatch):
    attempt = quiz.start(alice, Kind.DATES, rng=random.Random(4))
    start = timezone.now()
    monkeypatch.setattr(timezone, "now", lambda: start)
    quiz.mark_shown(attempt)
    monkeypatch.setattr(timezone, "now", lambda: start + datetime.timedelta(seconds=30))
    attempt = quiz.mark_shown(attempt)
    assert quiz.elapsed(attempt, 0) == 30
    attempt, *_ = quiz.answer(attempt, attempt.questions[0]["answer"])
    assert attempt.timings[0]["seconds"] == 30 and attempt.points == 1


def test_untimed_kinds_earn_a_point_per_right_answer(album, alice):
    attempt = play(alice, Kind.KINSHIP)
    assert attempt.points == attempt.correct == 10
    assert scoring.total(alice) == 10 + quiz.PERFECT_BONUS


# --- Where is it? -------------------------------------------------------------------------


def test_where_is_it_asks_about_accepted_locations(album, alice):
    attempt = quiz.start(alice, Kind.LOCATIONS, rng=random.Random(6))
    names = {name for name, *_rest in PLACES}
    for question in attempt.questions:
        photo = Photo.objects.get(pk=question["photo_id"])
        assert question["options"][question["answer"]]["label"] == photo.location.name
        assert {option["label"] for option in question["options"]} <= names
    # 5 places for 10 questions: each place comes up twice, not more.
    answers = [q["options"][q["answer"]]["label"] for q in attempt.questions]
    assert max(answers.count(name) for name in names) == 2


def test_where_is_it_offers_the_same_town_first(album, alice):
    for seed in range(5):
        attempt = quiz.start(alice, Kind.LOCATIONS, rng=random.Random(seed))
        for question in attempt.questions:
            labels = {option["label"] for option in question["options"]}
            if "Casa de la abuela" in labels and question["options"][question["answer"]][
                "label"
            ] in ("Casa de la abuela", "Plaza de toros"):
                assert "Plaza de toros" in labels


def test_where_is_it_needs_four_places_and_ten_photos(album, alice):
    Location.objects.filter(name__in=["Sierra Nevada", "Puerto de Cádiz"]).update(active=False)
    Photo.objects.filter(location__active=False).update(location=None)
    info = quiz.availability(Kind.LOCATIONS)
    assert not info.ready and "Faltan fotos con ubicación: hay 0 de 10." == str(info.missing)


def test_where_is_it_skips_pending_locations_and_follows_merges(album, alice):
    pending = place("Cortijo", is_provisional=True)
    merged = place("Casa abuela (vieja)")
    Location.objects.filter(pk=merged.pk).update(
        merged_into=Location.objects.get(name="Casa de la abuela")
    )
    photos = list(Photo.objects.all()[:2])
    Photo.objects.filter(pk=photos[0].pk).update(location=pending)
    Photo.objects.filter(pk=photos[1].pk).update(location=merged)
    pool = quiz._locations_pool()
    assert pending not in pool and merged not in pool
    assert photos[1].pk in pool[Location.objects.get(name="Casa de la abuela")]
    assert photos[0].pk not in {pid for photos in pool.values() for pid in photos}


# --- Who is hiding? (timed) ---------------------------------------------------------------


@pytest.fixture
def clock(monkeypatch):
    """A controllable `timezone.now`: clock.advance(seconds)."""

    class Clock:
        now = timezone.now()

        def advance(self, seconds):
            self.now += datetime.timedelta(seconds=seconds)

    clock = Clock()
    monkeypatch.setattr(timezone, "now", lambda: clock.now)
    return clock


def test_who_is_hiding_frames_a_face_of_the_right_person(album, alice):
    attempt = quiz.start(alice, Kind.HIDDEN, rng=random.Random(8))
    assert len(attempt.questions) == 10
    for question in attempt.questions:
        photo = Photo.objects.get(pk=question["photo_id"])
        right = question["options"][question["answer"]]["label"]
        assert photo.faces.filter(person__display_name=right).exists()
        assert set(question["frame"]) == {"left", "top", "width", "height"}
        assert "photo_id" not in quiz.public_question(question)


def test_people_beside_the_framed_face_are_not_offered(album, alice):
    ana, luis = album[0], album[1]
    Face.objects.create(photo=ana.faces.get().photo, x=300, y=10, w=50, h=50, person=luis)
    for seed in range(10):
        attempt = quiz.start(alice, Kind.HIDDEN, rng=random.Random(seed))
        for question in attempt.questions:
            labels = {option["label"] for option in question["options"]}
            if question["options"][question["answer"]]["label"] == "Ana":
                assert "Luis" not in labels


@pytest.mark.parametrize(("seconds", "points"), [(3, 3), (7.9, 3), (8, 2), (15, 2), (40, 1)])
def test_faster_right_answers_earn_more(album, alice, clock, seconds, points):
    attempt = quiz.mark_shown(quiz.start(alice, Kind.HIDDEN, rng=random.Random(1)))
    clock.advance(seconds)
    attempt, *_ = quiz.answer(attempt, attempt.questions[0]["answer"])
    assert attempt.points == points
    assert quiz.answer_feedback(attempt, 0) == (points, seconds)


def test_wrong_and_unshown_answers_in_timed_quizzes(album, alice, clock):
    attempt = quiz.mark_shown(quiz.start(alice, Kind.HIDDEN, rng=random.Random(1)))
    wrong = (attempt.questions[0]["answer"] + 1) % 4
    attempt, *_ = quiz.answer(attempt, wrong)
    assert attempt.points == 0
    # Never shown (no time recorded): a right answer counts as a slow one.
    attempt, *_ = quiz.answer(attempt, attempt.questions[1]["answer"])
    assert attempt.points == 1


def test_a_perfect_fast_quiz_scores_thirty_plus_the_bonus(album, alice, clock):
    attempt = quiz.start(alice, Kind.HIDDEN, rng=random.Random(1))
    for question in attempt.questions:
        attempt = quiz.mark_shown(attempt)
        clock.advance(2)
        attempt, *_ = quiz.answer(attempt, question["answer"])
    assert attempt.points == 30
    assert scoring.total(alice) == 30 + quiz.PERFECT_BONUS


def test_the_photo_sharpens_only_as_time_goes_by(client, photo_files, alice, clock):
    client.force_login(alice)
    attempt = quiz.start(alice, Kind.HIDDEN, rng=random.Random(1))
    page = client.get(reverse("quiz_play", args=[attempt.pk])).content.decode()
    assert reverse("quiz_image", args=[attempt.pk, 0, 0]) in page
    assert "data-quiz-reveal" in page and "quiz-frame" in page

    def status(level):
        return client.get(reverse("quiz_image", args=[attempt.pk, 0, level])).status_code

    assert (status(0), status(1), status(5)) == (200, 403, 403)
    clock.advance(4.5)
    assert (status(1), status(2)) == (200, 403)
    # Numbers for the script are never localized («4,5» would be NaN in JS).
    page = client.get(reverse("quiz_play", args=[attempt.pk])).content.decode()
    assert 'data-elapsed="4.5"' in page and 'data-until="8"' in page
    clock.advance(15.5)
    assert status(5) == 200

    # Reloading does not restart the clock.
    page = client.get(reverse("quiz_play", args=[attempt.pk])).content.decode()
    assert reverse("quiz_image", args=[attempt.pk, 0, 5]) in page


def test_feedback_shows_the_points_and_the_time(client, photo_files, alice, clock):
    client.force_login(alice)
    attempt = quiz.start(alice, Kind.HIDDEN, rng=random.Random(1))
    client.get(reverse("quiz_play", args=[attempt.pk]))
    clock.advance(5)
    answered = client.post(
        reverse("quiz_answer", args=[attempt.pk]), {"option": attempt.questions[0]["answer"]}
    ).content.decode()
    assert "+3 puntos" in answered and "5,0 s" in answered
    assert reverse("quiz_image", args=[attempt.pk, 0, 5]) in answered


# --- Emojis (timed matching) --------------------------------------------------------------


@pytest.fixture
def group_photo(album):
    """Ana and Luis (with emojis), Elena (no nickname) and an unknown face, in one photo."""
    ana, luis, elena = album[0], album[1], album[10]
    photo = Photo.objects.create(
        scan=Scan.objects.get(), file_name="quiz_group.jpg", index=99, width=800, height=600
    )
    for x, person in ((10, ana), (200, luis), (400, elena), (600, None)):
        Face.objects.create(photo=photo, x=x, y=10, w=100, h=100, person=person)
    return photo


def group_question(alice, group_photo):
    for seed in range(40):
        attempt = quiz.start(alice, Kind.EMOJIS, rng=random.Random(seed))
        for index, question in enumerate(attempt.questions):
            if question["photo_id"] == group_photo.pk:
                return attempt, index, question
    raise AssertionError("the group photo never came up")


def test_emojis_cover_every_face_and_ask_only_own_emojis(album, alice, group_photo):
    _attempt, _index, question = group_question(alice, group_photo)
    covers = question["covers"]
    assert [c["emoji"] for c in covers] == ["🐦", "🍷", quiz.GENERIC_EMOJI, quiz.GENERIC_EMOJI]
    assert [c["number"] for c in covers] == [1, 2, None, None]
    labels = [option["label"] for option in question["options"]]
    assert [labels[i] for i in question["answer"]] == ["Ana", "Luis"]
    assert len(labels) == len(set(labels)) == 4  # the two asked about and two lures
    assert "photo_id" not in quiz.public_question(question)


def test_emojis_questions_have_at_least_four_names(album, alice):
    attempt = quiz.start(alice, Kind.EMOJIS, rng=random.Random(3))
    assert len(attempt.questions) == 10
    for question in attempt.questions:
        labels = [option["label"] for option in question["options"]]
        assert len(labels) >= 4 and len(set(labels)) == len(labels)
        assert len(question["answer"]) == sum(1 for c in question["covers"] if c["number"])


def test_emojis_need_photos_of_nicknames_with_emojis(album, alice):
    PersonNickname.objects.update(emoji="")
    info = quiz.availability(Kind.EMOJIS)
    assert not info.ready and "hay 0 de 10" in str(info.missing)


def test_a_match_is_right_only_if_every_face_is(album, alice, group_photo, clock):
    attempt, index, question = group_question(alice, group_photo)
    for _ in range(index):
        attempt = quiz.mark_shown(attempt)
        attempt, *_ = quiz.answer(attempt, attempt.questions[len(attempt.answers)]["answer"])
    attempt = quiz.mark_shown(attempt)
    before = attempt.correct
    with pytest.raises(ValidationError, match="Elige una persona para cada emoji"):
        quiz.answer(attempt, question["answer"][:1])
    swapped = list(reversed(question["answer"]))
    attempt, _question, chosen, right = quiz.answer(attempt, swapped)
    assert not right and chosen == swapped and attempt.correct == before


def test_play_emojis_through_the_screens(client, photo_files, alice, group_photo, clock):
    client.force_login(alice)
    attempt, index, question = group_question(alice, group_photo)
    for earlier in attempt.questions[:index]:
        quiz.answer(quiz.mark_shown(attempt), earlier["answer"])
    page = client.get(reverse("quiz_play", args=[attempt.pk])).content.decode()
    assert 'name="match"' in page and "🐦" in page and "data-quiz-match" in page
    assert reverse("quiz_image", args=[attempt.pk, index, 0]) in page
    clock.advance(3)
    answered = client.post(
        reverse("quiz_answer", args=[attempt.pk]), {"match": question["answer"]}
    ).content.decode()
    assert "¡Correcto!" in answered and "+3 puntos" in answered
    assert answered.count('class="quiz-match right"') == 2
