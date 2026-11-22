"""Family quiz (spec section 5.14): 10 questions with 4 options built from accepted data.

Four kinds: who is this face, whose nickname is this, which year (or decade) is this photo
and what is one relative of another. A kind is available only when there is data for 10
questions with 4 different options. The right answer is stored with the attempt and is
only shown after answering.

Points: 1 per right answer and a bonus for 10/10, only for the first quizzes of each kind
per day (decided when the quiz starts) and only for players (scoring.award).
"""

import random
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from gamification.models import Achievement, QuizAttempt, ScoreEvent
from gamification.services import scoring

Kind = QuizAttempt.Kind

QUESTIONS = 10
OPTIONS = 4
DAILY_SCORED = 3  # quizzes of each kind per day that give points
PERFECT_BONUS = 10
DATE_SPREAD = 8  # wrong years within ±8 of the right one
CLOSE_ERA = 6  # distractors for faces are drawn among the 6 closest in time

KIND_LABELS = {
    Kind.FACES: gettext_lazy("¿Quién es?"),
    Kind.NICKNAMES: gettext_lazy("Apodos"),
    Kind.DATES: gettext_lazy("Fechas"),
    Kind.KINSHIP: gettext_lazy("Parentescos"),
}
KIND_HINTS = {
    Kind.FACES: gettext_lazy("Adivina quién sale en cada cara."),
    Kind.NICKNAMES: gettext_lazy("¿De quién es cada apodo?"),
    Kind.DATES: gettext_lazy("¿De qué año o década es cada foto?"),
    Kind.KINSHIP: gettext_lazy("¿Qué es cada familiar de otro?"),
}
KIND_ICONS = {
    Kind.FACES: "face",
    Kind.NICKNAMES: "badge",
    Kind.DATES: "calendar_month",
    Kind.KINSHIP: "family_history",
}
# What a kind needs, shown while it is not available ("Faltan apodos: hay 4 de 10").
KIND_MISSING = {
    Kind.FACES: gettext_lazy("Faltan personas identificadas: hay %(have)s de %(need)s."),
    Kind.NICKNAMES: gettext_lazy("Faltan apodos: hay %(have)s de %(need)s."),
    Kind.DATES: gettext_lazy("Faltan fotos con fecha: hay %(have)s de %(need)s."),
    Kind.KINSHIP: gettext_lazy("Faltan parentescos: hay %(have)s de %(need)s."),
}


def perfect_reason(kind):
    return f"quiz_perfect_{kind}"


# ---------------------------------------------------------------------------
# Data pools (accepted data only)
# ---------------------------------------------------------------------------


def _people():
    from catalog.models import Person

    return Person.objects.active().filter(is_provisional=False)


def _face_url(face_id):
    return reverse("face_image", args=[face_id]) if face_id else ""


def _avatars(person_ids):
    from catalog.services.avatars import avatar_faces

    return avatar_faces(list(person_ids))


def _faces_pool():
    """{person_id: [face_id, ...]} of accepted faces in visible photos, and each person's
    era (mean year of their dated photos, None if none is dated)."""
    from catalog.models import Face

    faces = Face.objects.filter(
        status=Face.Status.ACTIVE,
        person__in=_people(),
        photo__discarded=False,
    ).values_list("pk", "person_id", "photo__date_value")
    by_person, years = {}, {}
    for face_id, person_id, date_value in faces:
        by_person.setdefault(person_id, []).append(face_id)
        if date_value:
            years.setdefault(person_id, []).append(date_value.year)
    era = {pid: sum(ys) / len(ys) for pid, ys in years.items()}
    # Pets are asked about among pets, people among people (spec 5.16): a group with
    # fewer than 4 cannot make a question, so it is left out.
    pets = set(_people().filter(pk__in=by_person, is_pet=True).values_list("pk", flat=True))
    for group in (pets, set(by_person) - pets):
        if len(group) < OPTIONS:
            for person_id in group:
                by_person.pop(person_id, None)
    return by_person, era, pets & set(by_person)


def _nicknames_pool():
    """[(text, person_id)] of nicknames that belong to a single person."""
    from catalog.models import PersonNickname
    from catalog.services.names import name_key

    owners = {}
    rows = PersonNickname.objects.filter(person__in=_people()).values_list(
        "text", "text_key", "person_id"
    )
    for text, key, person_id in rows:
        owners.setdefault(key or name_key(text), []).append((text, person_id))
    return [items[0] for items in owners.values() if len({p for _t, p in items}) == 1]


def _dates_pool():
    from catalog.models import Photo

    return list(
        Photo.objects.visible()
        .filter(date_value__isnull=False)
        .exclude(date_precision="")
        .only("pk", "date_value", "date_precision", "image_version")
    )


def _kinship_pool():
    """[(other_id, subject_id, role)]: «other is <role> of subject», explicit or deduced,
    family only, one per pair of people."""
    from catalog.services import kinship

    people = set(_people().values_list("pk", flat=True))
    graph = kinship.Graph.load()
    pairs, seen = [], set()

    def add(other, subject, role):
        key = frozenset((other, subject))
        if other in people and subject in people and key not in seen and role in kinship.ROLES:
            seen.add(key)
            pairs.append((other, subject, role))

    nodes = set()
    for person_from, person_to, kind, _activity in graph.edges:
        nodes.update((person_from, person_to))
        if kinship.tree_of(kind) == kinship.FAMILY:
            add(person_to, person_from, kinship.role_of_other(kind, True))
    for subject in sorted(nodes & people):
        for other, role in graph.deduced(subject).items():
            add(other, subject, role)
    return pairs


@dataclass
class Availability:
    kind: str
    have: int
    need: int

    @property
    def ready(self):
        return self.have >= self.need

    @property
    def missing(self):
        return KIND_MISSING[self.kind] % {"have": self.have, "need": self.need}


def availability(kind):
    """How much usable data there is for `kind` (one question per item)."""
    if kind == Kind.FACES:
        by_person, _era, _pets = _faces_pool()
        have = len(by_person)
    elif kind == Kind.NICKNAMES:
        people = _people().count()
        have = len(_nicknames_pool()) if people >= OPTIONS else 0
    elif kind == Kind.DATES:
        have = len(_dates_pool())
    else:
        have = len(_kinship_pool())
    return Availability(kind, have, QUESTIONS)


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------


def _question(text, right, wrong, rng, images=()):
    """`right` and `wrong` are option dicts ({label, avatar}); the right one is shuffled
    among them and its index kept."""
    options = [right, *wrong[: OPTIONS - 1]]
    rng.shuffle(options)
    return {
        "text": str(text),
        "images": list(images),
        "options": options,
        "answer": options.index(right),
    }


def _person_option(person, avatars, with_avatar=True):
    return {
        "label": person.shown_name,
        "avatar": _face_url(avatars.get(person.pk)) if with_avatar else "",
    }


def _distinct_names(right, candidates, shuffle=None):
    """Candidates whose shown name differs from the right one and from each other (names
    repeat in a family: two «Ana» options would be unanswerable). Order is kept."""
    candidates = list(candidates)
    if shuffle is not None:
        shuffle.shuffle(candidates)
    seen = {right.shown_name.casefold()}
    result = []
    for person in candidates:
        key = person.shown_name.casefold()
        if person.pk != right.pk and key not in seen:
            seen.add(key)
            result.append(person)
    return result


def _faces_questions(rng):
    by_person, era, pets = _faces_pool()
    people = _people().in_bulk(list(by_person))
    chosen = rng.sample(sorted(by_person), QUESTIONS)
    questions = []
    for person_id in chosen:
        mine = era.get(person_id)
        is_pet = person_id in pets
        others = [pid for pid in by_person if pid != person_id and (pid in pets) == is_pet]
        # Plausible: people of the same time first (undated people last), then random.
        others.sort(
            key=lambda pid, mine=mine: (
                abs(era[pid] - mine) if mine and pid in era else 1e9,
                rng.random(),
            )
        )
        others = _distinct_names(people[person_id], [people[pid] for pid in others])
        closest = others[: max(CLOSE_ERA, OPTIONS - 1)]
        wrong = [p.pk for p in rng.sample(closest, min(len(closest), OPTIONS - 1))]
        face_id = rng.choice(by_person[person_id])
        questions.append(
            _question(
                _("¿Quién es?"),
                _person_option(people[person_id], {}, with_avatar=False),
                [_person_option(people[pid], {}, with_avatar=False) for pid in wrong],
                rng,
                images=[_face_url(face_id)],
            )
        )
    return questions


def _nicknames_questions(rng):
    pool = _nicknames_pool()
    chosen = rng.sample(pool, QUESTIONS)
    people = list(_people().only("pk", "display_name", "honorific"))
    avatars = _avatars([p.pk for p in people])
    with_face = [p for p in people if p.pk in avatars]
    questions = []
    for text, owner_id in chosen:
        owner = next(p for p in people if p.pk == owner_id)
        candidates = _distinct_names(
            owner, with_face if len(with_face) > OPTIONS else people, shuffle=rng
        )
        wrong = rng.sample(candidates, min(len(candidates), OPTIONS - 1))
        questions.append(
            _question(
                _("¿De quién es el apodo «%(nickname)s»?") % {"nickname": text},
                _person_option(owner, avatars),
                [_person_option(p, avatars) for p in wrong],
                rng,
            )
        )
    return questions


def _year_options(year, step, rng):
    """Wrong years (or decades) near the right one, never in the future."""
    this_year = timezone.localdate().year
    near = [year + step * d for d in range(-DATE_SPREAD, DATE_SPREAD + 1) if d]
    if step == 10:
        near = [year + 10 * d for d in (-3, -2, -1, 1, 2, 3)]
    near = [value for value in near if value <= this_year and value > 1800]
    rng.shuffle(near)
    return near[: OPTIONS - 1]


def _dates_questions(rng):
    from catalog.models import Photo

    chosen = rng.sample(_dates_pool(), QUESTIONS)
    questions = []
    for photo in chosen:
        image = f"{reverse('photo_image', args=[photo.pk, 400])}?v={photo.image_version}"
        if photo.date_precision == Photo.DatePrecision.DECADE:
            decade = photo.date_value.year // 10 * 10

            def label(value):
                return _("Década de %(decade)s") % {"decade": value}

            right, wrong = decade, _year_options(decade, 10, rng)
            text = _("¿De qué década es esta foto?")
        else:

            def label(value):
                return str(value)

            right, wrong = photo.date_value.year, _year_options(photo.date_value.year, 1, rng)
            text = _("¿De qué año es esta foto?")
        questions.append(
            _question(
                text,
                {"label": label(right), "avatar": ""},
                [{"label": label(value), "avatar": ""} for value in wrong],
                rng,
                images=[image],
            )
        )
    return questions


def _kinship_questions(rng):
    from catalog.services import kinship

    chosen = rng.sample(_kinship_pool(), QUESTIONS)
    ids = {pid for other, subject, _role in chosen for pid in (other, subject)}
    people = _people().in_bulk(list(ids))
    avatars = _avatars(ids)
    labels = {role: str(kinship.role_label(role)) for role in kinship.FAMILY_ROLES}
    questions = []
    for other, subject, role in chosen:
        right = str(kinship.role_label(role))
        wrong = sorted({label for label in labels.values() if label != right})
        rng.shuffle(wrong)
        questions.append(
            _question(
                _("¿Qué es %(other)s de %(subject)s?")
                % {"other": people[other].shown_name, "subject": people[subject].shown_name},
                {"label": right, "avatar": ""},
                [{"label": label, "avatar": ""} for label in wrong],
                rng,
                images=[_face_url(avatars.get(other)), _face_url(avatars.get(subject))],
            )
        )
    return questions


BUILDERS = {
    Kind.FACES: _faces_questions,
    Kind.NICKNAMES: _nicknames_questions,
    Kind.DATES: _dates_questions,
    Kind.KINSHIP: _kinship_questions,
}


# ---------------------------------------------------------------------------
# Playing
# ---------------------------------------------------------------------------


def scored_today(user, kind):
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    return QuizAttempt.objects.filter(
        user=user, kind=kind, scored=True, created_at__gte=start
    ).count()


def scored_left(user, kind):
    """Quizzes of `kind` that still give points today (0 for non-players)."""
    if not user.is_player:
        return 0
    return max(0, DAILY_SCORED - scored_today(user, kind))


@transaction.atomic
def start(user, kind, rng=None):
    if kind not in BUILDERS:
        raise ValidationError(_("Ese quiz no existe."))
    info = availability(kind)
    if not info.ready:
        raise ValidationError(info.missing)
    rng = rng or random.Random()
    return QuizAttempt.objects.create(
        user=user,
        kind=kind,
        questions=BUILDERS[kind](rng),
        scored=scored_left(user, kind) > 0,
    )


def current_index(attempt):
    return len(attempt.answers)


@transaction.atomic
def answer(attempt, option):
    """Record the answer to the current question; finishes the quiz after the last one.
    Returns (question, chosen index, right?)."""
    attempt = QuizAttempt.objects.select_for_update().get(pk=attempt.pk)
    if attempt.is_finished:
        raise ValidationError(_("Este quiz ya ha terminado."))
    index = current_index(attempt)
    question = attempt.questions[index]
    try:
        option = int(option)
    except (TypeError, ValueError):
        option = -1
    if not 0 <= option < len(question["options"]):
        raise ValidationError(_("Elige una de las opciones."))
    right = option == question["answer"]
    attempt.answers = [*attempt.answers, option]
    attempt.correct += int(right)
    fields = ["answers", "correct"]
    if len(attempt.answers) == len(attempt.questions):
        attempt.finished_at = timezone.now()
        fields.append("finished_at")
    attempt.save(update_fields=fields)
    if attempt.is_finished:
        award(attempt)
    return attempt, question, option, right


def award(attempt, notify=True):
    """Points of a finished, scored quiz: 1 per right answer and a bonus for 10/10."""
    if not (attempt.scored and attempt.is_finished):
        return
    when = attempt.finished_at
    if attempt.correct:
        scoring.award(attempt.user, "quiz", attempt.correct, attempt, when=when, notify=notify)
    if attempt.correct == len(attempt.questions):
        scoring.award(
            attempt.user,
            perfect_reason(attempt.kind),
            PERFECT_BONUS,
            attempt,
            when=when,
            notify=notify,
        )


def result(attempt):
    """Points earned by a finished quiz and the badges unlocked with it."""
    from gamification.services.achievements import BY_CODE

    ref_type = attempt._meta.label_lower
    points = sum(
        ScoreEvent.objects.filter(
            user=attempt.user, ref_type=ref_type, ref_id=str(attempt.pk)
        ).values_list("points", flat=True)
    )
    codes = Achievement.objects.filter(
        user=attempt.user, unlocked_at__gte=attempt.finished_at
    ).values_list("code", flat=True)
    return points, [BY_CODE[code] for code in codes if code in BY_CODE]
