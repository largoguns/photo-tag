"""Family quiz (spec section 5.14): 10 questions with 4 options built from accepted data.

Kinds: who is this face, whose nickname is this, which year (or decade) is this photo,
what is one relative of another, where was this photo taken, who is hiding in a
pixelated photo that sharpens with time and who is behind each emoji (the last two are
timed: faster right answers earn more). A kind is available only when there is data for 10
questions with 4 different options. The right answer is stored with the attempt and is
only shown after answering.

Points: 1 per right answer (3/2/1 by speed in timed kinds) and a bonus for 10/10, only
for the first quizzes of each kind per day (decided when the quiz starts) and only for
players (scoring.award).

Photos are shown through the quiz's own image view (`quiz_image`), never by photo id: the
id would lead to the photo's page and its answer. The id stays in the private part of the
question (PRIVATE_KEYS), with the answer.
"""

import random
from dataclasses import dataclass
from datetime import datetime

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
CLOSE_PLACES = 5  # distractors for locations are drawn among the 5 most alike

# Timed kinds: the photo sharpens one pixelation level every REVEAL_STEP seconds and a
# right answer earns 3, 2 or 1 points depending on how fast it came.
TIMED = frozenset({Kind.HIDDEN, Kind.EMOJIS})
REVEAL_STEP = 4
SPEED_POINTS = ((8, 3), (16, 2))  # (answered before N seconds, points); 1 after that
GENERIC_EMOJI = "🙂"  # covers faces without an emoji of their own (not asked about)
MAX_MATCHES = 6  # faces to match in one photo at most
LURES = 2  # names offered besides those of the people asked about (at least)
# Never sent to the browser before answering (and the photo id never at all).
PRIVATE_KEYS = frozenset({"answer", "photo_id"})

# Order of the cards on the quiz page: related kinds side by side.
ORDER = (
    Kind.FACES,
    Kind.HIDDEN,
    Kind.NICKNAMES,
    Kind.EMOJIS,
    Kind.DATES,
    Kind.LOCATIONS,
    Kind.KINSHIP,
)
KIND_LABELS = {
    Kind.FACES: gettext_lazy("¿Quién es?"),
    Kind.NICKNAMES: gettext_lazy("Apodos"),
    Kind.DATES: gettext_lazy("Fechas"),
    Kind.KINSHIP: gettext_lazy("Parentescos"),
    Kind.LOCATIONS: gettext_lazy("¿Dónde es?"),
    Kind.HIDDEN: gettext_lazy("¿Quién se esconde?"),
    Kind.EMOJIS: gettext_lazy("Emojis"),
}
KIND_HINTS = {
    Kind.FACES: gettext_lazy("Adivina quién sale en cada cara."),
    Kind.NICKNAMES: gettext_lazy("¿De quién es cada apodo?"),
    Kind.DATES: gettext_lazy("¿De qué año o década es cada foto?"),
    Kind.KINSHIP: gettext_lazy("¿Qué es cada familiar de otro?"),
    Kind.LOCATIONS: gettext_lazy("¿Dónde se hizo cada foto?"),
    Kind.HIDDEN: gettext_lazy("La foto se va aclarando: cuanto antes aciertes, más puntos."),
    Kind.EMOJIS: gettext_lazy("¿Quién se esconde detrás de cada emoji?"),
}
KIND_ICONS = {
    Kind.FACES: "face",
    Kind.NICKNAMES: "badge",
    Kind.DATES: "calendar_month",
    Kind.KINSHIP: "family_history",
    Kind.LOCATIONS: "location_on",
    Kind.HIDDEN: "blur_on",
    Kind.EMOJIS: "mood",
}
# What a kind needs, shown while it is not available ("Faltan apodos: hay 4 de 10").
KIND_MISSING = {
    Kind.FACES: gettext_lazy("Faltan personas identificadas: hay %(have)s de %(need)s."),
    Kind.NICKNAMES: gettext_lazy("Faltan apodos: hay %(have)s de %(need)s."),
    Kind.DATES: gettext_lazy("Faltan fotos con fecha: hay %(have)s de %(need)s."),
    Kind.KINSHIP: gettext_lazy("Faltan parentescos: hay %(have)s de %(need)s."),
    Kind.LOCATIONS: gettext_lazy("Faltan fotos con ubicación: hay %(have)s de %(need)s."),
    Kind.HIDDEN: gettext_lazy("Faltan personas identificadas: hay %(have)s de %(need)s."),
    Kind.EMOJIS: gettext_lazy("Faltan fotos con apodos con emoji: hay %(have)s de %(need)s."),
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


def _accepted_faces():
    from catalog.models import Face

    return Face.objects.filter(
        status=Face.Status.ACTIVE, person__in=_people(), photo__discarded=False
    )


def _faces_pool():
    """{person_id: [face_id, ...]} of accepted faces in visible photos, and each person's
    era (mean year of their dated photos, None if none is dated)."""
    faces = _accepted_faces().values_list("pk", "person_id", "photo__date_value")
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
        .only("pk", "date_value", "date_precision")
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


def _locations_pool():
    """{location: [photo_id, ...]} of visible photos whose location is accepted (merged
    locations count as the one they were merged into)."""
    from catalog.models import Photo

    by_location = {}
    photos = Photo.objects.visible().filter(location__isnull=False).select_related("location")
    for photo in photos.only("pk", "location"):
        location = photo.location.resolved()
        if location.active and not location.is_provisional:
            by_location.setdefault(location, []).append(photo.pk)
    return by_location


def _emojis_of_people():
    """{person_id: [emoji, ...]} of accepted people with nicknames that have emojis."""
    from catalog.models import PersonNickname

    emojis = {}
    rows = PersonNickname.objects.filter(person__in=_people()).exclude(emoji="")
    for person_id, emoji in rows.values_list("person_id", "emoji"):
        emojis.setdefault(person_id, []).append(emoji)
    return emojis


def _emojis_pool():
    """[photo_id, ...] of visible photos with an accepted face of someone whose nickname
    has an emoji."""
    return sorted(
        set(
            _accepted_faces()
            .filter(person__in=list(_emojis_of_people()))
            .values_list("photo_id", flat=True)
        )
    )


def _places():
    from catalog.models import Location

    return list(Location.objects.enabled())


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
    if kind in (Kind.FACES, Kind.HIDDEN):
        by_person, _era, _pets = _faces_pool()
        have = len(by_person)
    elif kind == Kind.NICKNAMES:
        people = _people().count()
        have = len(_nicknames_pool()) if people >= OPTIONS else 0
    elif kind == Kind.DATES:
        have = len(_dates_pool())
    elif kind == Kind.EMOJIS:
        have = len(_emojis_pool()) if _people().count() >= OPTIONS else 0
    elif kind == Kind.LOCATIONS:
        pool = _locations_pool()
        enough = len({place.name.casefold() for place in _places()}) >= OPTIONS
        have = sum(len(photos) for photos in pool.values()) if enough else 0
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


def _face_distractors(person_id, by_person, era, pets, people, rng, exclude=()):
    """Wrong options for a face of `person_id`: people of the same time first (undated
    people last), pets among pets, never namesakes nor anyone in `exclude`."""
    mine = era.get(person_id)
    is_pet = person_id in pets
    others = [
        pid
        for pid in by_person
        if pid != person_id and pid not in exclude and (pid in pets) == is_pet
    ]
    others.sort(
        key=lambda pid: (abs(era[pid] - mine) if mine and pid in era else 1e9, rng.random())
    )
    others = _distinct_names(people[person_id], [people[pid] for pid in others])
    closest = others[: max(CLOSE_ERA, OPTIONS - 1)]
    return rng.sample(closest, min(len(closest), OPTIONS - 1))


def _faces_questions(rng):
    by_person, era, pets = _faces_pool()
    people = _people().in_bulk(list(by_person))
    chosen = rng.sample(sorted(by_person), QUESTIONS)
    questions = []
    for person_id in chosen:
        wrong = _face_distractors(person_id, by_person, era, pets, people, rng)
        face_id = rng.choice(by_person[person_id])
        questions.append(
            _question(
                _("¿Quién es?"),
                _person_option(people[person_id], {}, with_avatar=False),
                [_person_option(p, {}, with_avatar=False) for p in wrong],
                rng,
                images=[_face_url(face_id)],
            )
        )
    return questions


def _hidden_questions(rng):
    """A whole photo, pixelated, with one face framed: «¿Quién se esconde aquí?»."""
    from catalog.models import Face

    by_person, era, pets = _faces_pool()
    people = _people().in_bulk(list(by_person))
    chosen = rng.sample(sorted(by_person), QUESTIONS)
    faces = Face.objects.select_related("photo").in_bulk(
        [rng.choice(by_person[pid]) for pid in chosen]
    )
    face_of = {face.person_id: face for face in faces.values()}
    questions = []
    for person_id in chosen:
        face = face_of[person_id]
        # Whoever else is in the photo is not offered: the frame says which face it is.
        beside = set(
            _accepted_faces().filter(photo=face.photo_id).values_list("person_id", flat=True)
        )
        wrong = _face_distractors(person_id, by_person, era, pets, people, rng, exclude=beside)
        question = _question(
            _("¿Quién se esconde aquí?"),
            _person_option(people[person_id], {}, with_avatar=False),
            [_person_option(p, {}, with_avatar=False) for p in wrong],
            rng,
        )
        box = {key: round(value, 2) for key, value in face.as_percent().items()}
        questions.append({**question, "photo_id": face.photo_id, "frame": box})
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
        question = _question(
            text,
            {"label": label(right), "avatar": ""},
            [{"label": label(value), "avatar": ""} for value in wrong],
            rng,
        )
        questions.append({**question, "photo_id": photo.pk})
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


def _place_distance(a, b):
    """Rough distance between two placed locations (degrees; only used to sort)."""
    if not (a.is_placed and b.is_placed):
        return 1e9
    return ((a.latitude - b.latitude) ** 2 + (a.longitude - b.longitude) ** 2) ** 0.5


def _spread_photos(by_location, rng, count):
    """`count` photos taking turns between locations, so the same place repeats only
    when there are not enough places."""
    queues = [rng.sample(photos, len(photos)) for photos in by_location.values()]
    owner = {pid: place for place, photos in by_location.items() for pid in photos}
    rng.shuffle(queues)
    chosen = []
    while len(chosen) < count and any(queues):
        for queue in queues:
            if queue and len(chosen) < count:
                chosen.append(queue.pop())
    return [(pid, owner[pid]) for pid in chosen]


def _locations_questions(rng):
    places = _places()
    questions = []
    for photo_id, place in _spread_photos(_locations_pool(), rng, QUESTIONS):
        locality = place.locality.strip().casefold()
        others = [p for p in places if p.pk != place.pk]
        # Plausible: same town first, then the nearest on the map, then any.
        others.sort(
            key=lambda p, place=place, locality=locality: (
                not (locality and p.locality.strip().casefold() == locality),
                _place_distance(place, p),
                rng.random(),
            )
        )
        seen, alike = {place.name.casefold()}, []
        for other in others:
            if other.name.casefold() not in seen:
                seen.add(other.name.casefold())
                alike.append(other)
        # The most alike always, the rest drawn among the next ones (varied quizzes).
        closest = alike[1 : max(CLOSE_PLACES, OPTIONS - 1)]
        wrong = alike[:1] + rng.sample(closest, min(len(closest), OPTIONS - 2))
        question = _question(
            _("¿Dónde se hizo esta foto?"),
            {"label": place.name, "avatar": ""},
            [{"label": other.name, "avatar": ""} for other in wrong],
            rng,
        )
        questions.append({**question, "photo_id": photo_id})
    return questions


def _emojis_questions(rng):
    """A whole photo, pixelated, with every face covered by an emoji: the person's own
    (one of their nicknames') or a generic one. Each face with its own emoji is matched
    to a person; the answer is the list of right options, in the order of those faces."""
    from catalog.models import Face, Photo

    emojis = _emojis_of_people()
    people = {p.pk: p for p in _people().only("pk", "display_name", "honorific")}
    pool = _emojis_pool()
    photos = Photo.objects.in_bulk(pool)
    # Group photos first (more to match), then random; 10 different photos.
    faces_of = {}
    visible = Face.objects.filter(
        photo__in=pool, status__in=[Face.Status.ACTIVE, Face.Status.PROPOSED]
    ).order_by("x", "pk")
    for face in visible:
        faces_of.setdefault(face.photo_id, []).append(face)
    chosen = sorted(
        pool,
        key=lambda pid: (
            -min(MAX_MATCHES, len({f.person_id for f in faces_of[pid] if f.person_id in emojis})),
            rng.random(),
        ),
    )
    chosen = rng.sample(chosen[: QUESTIONS * 2], QUESTIONS)
    questions = []
    for photo_id in chosen:
        photo = photos[photo_id]
        covers, asked, names = [], [], set()
        for face in faces_of[photo_id]:
            person = people.get(face.person_id) if face.status == Face.Status.ACTIVE else None
            own = emojis.get(face.person_id) if person else None
            key = person.shown_name.casefold() if person else None
            ask = bool(own) and key not in names and len(asked) < MAX_MATCHES
            box = {k: round(v, 2) for k, v in face.as_percent().items()}
            if ask:
                names.add(key)
                asked.append(person)
            covers.append(
                {
                    "emoji": rng.choice(own) if own else GENERIC_EMOJI,
                    "box": box,
                    "number": len(asked) if ask else None,
                }
            )
        lures = _distinct_names(
            asked[0],
            [p for p in people.values() if p.pk not in {a.pk for a in asked}],
            shuffle=rng,
        )
        lures = [p for p in lures if p.shown_name.casefold() not in names]
        with_emoji = [p for p in lures if p.pk in emojis]
        count = max(LURES, OPTIONS - len(asked))  # at least 4 names to choose from
        lures = (with_emoji + [p for p in lures if p.pk not in emojis])[:count]
        options = [_person_option(p, {}, with_avatar=False) for p in [*asked, *lures]]
        rng.shuffle(options)
        labels = [option["label"] for option in options]
        questions.append(
            {
                "text": str(_("¿Quién es quién?")),
                "images": [],
                "options": options,
                "covers": covers,
                "answer": [labels.index(p.shown_name) for p in asked],
                "photo_id": photo.pk,
            }
        )
    return questions


BUILDERS = {
    Kind.FACES: _faces_questions,
    Kind.NICKNAMES: _nicknames_questions,
    Kind.DATES: _dates_questions,
    Kind.KINSHIP: _kinship_questions,
    Kind.LOCATIONS: _locations_questions,
    Kind.HIDDEN: _hidden_questions,
    Kind.EMOJIS: _emojis_questions,
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


def public_question(question):
    """What the browser may see of a question before it is answered."""
    return {key: value for key, value in question.items() if key not in PRIVATE_KEYS}


def _shown_at(attempt, index):
    timings = attempt.timings
    if index < len(timings) and timings[index].get("shown"):
        return datetime.fromisoformat(timings[index]["shown"])
    return None


@transaction.atomic
def mark_shown(attempt):
    """Record when the current question was first shown (reloading keeps the first time)
    and return the attempt, fresh from the database."""
    attempt = QuizAttempt.objects.select_for_update().get(pk=attempt.pk)
    index = current_index(attempt)
    if attempt.is_finished or _shown_at(attempt, index):
        return attempt
    timings = list(attempt.timings)
    timings.extend({} for _ in range(index + 1 - len(timings)))
    timings[index] = {**timings[index], "shown": timezone.now().isoformat()}
    attempt.timings = timings
    attempt.save(update_fields=["timings"])
    return attempt


def elapsed(attempt, index):
    """Seconds since question `index` was first shown (0 if it has not been yet)."""
    shown = _shown_at(attempt, index)
    return max(0.0, (timezone.now() - shown).total_seconds()) if shown else 0.0


def reveal_level(seconds):
    """Pixelation level reached after `seconds` (pixelate.SHARP once fully revealed)."""
    from gamification.services.pixelate import SHARP

    return min(SHARP, int(seconds // REVEAL_STEP))


def allowed_level(attempt, index):
    """Sharpest image level the player may see now for question `index`, or None if the
    question cannot be seen (not reached yet, or it has no photo)."""
    from gamification.services.pixelate import SHARP

    if not 0 <= index < len(attempt.questions) or "photo_id" not in attempt.questions[index]:
        return None
    current = current_index(attempt)
    if index < current or attempt.kind not in TIMED:
        return SHARP if index <= current else None
    if index > current:
        return None
    return reveal_level(elapsed(attempt, index))


def _checked_option(question, option):
    """The chosen option index, or the list of them when matching (one per face asked
    about, in order); ValidationError if it does not fit the question."""
    count = len(question["options"])
    matching = isinstance(question["answer"], list)
    values = option if matching else [option]
    if not isinstance(values, (list, tuple)):
        values = [values]
    try:
        values = [int(value) for value in values]
    except (TypeError, ValueError):
        values = []
    expected = len(question["answer"]) if matching else 1
    if len(values) != expected or not all(0 <= value < count for value in values):
        if matching:
            raise ValidationError(_("Elige una persona para cada emoji."))
        raise ValidationError(_("Elige una de las opciones."))
    return values if matching else values[0]


def speed_points(seconds):
    for limit, points in SPEED_POINTS:
        if seconds < limit:
            return points
    return 1


@transaction.atomic
def answer(attempt, option):
    """Record the answer to the current question; finishes the quiz after the last one.
    `option` is an option index, or a list of them for a matching question. Returns
    (attempt, question, chosen index or list, right?)."""
    attempt = QuizAttempt.objects.select_for_update().get(pk=attempt.pk)
    if attempt.is_finished:
        raise ValidationError(_("Este quiz ya ha terminado."))
    index = current_index(attempt)
    question = attempt.questions[index]
    option = _checked_option(question, option)
    right = option == question["answer"]
    shown = _shown_at(attempt, index)
    seconds = round(elapsed(attempt, index), 1) if shown else None
    timings = list(attempt.timings)
    timings.extend({} for _ in range(index + 1 - len(timings)))
    timings[index] = {**timings[index], "seconds": seconds}
    gained = 0
    if right:
        # A question never shown (no time recorded) counts as a slow answer.
        timed = attempt.kind in TIMED
        gained = speed_points(seconds if seconds is not None else 1e9) if timed else 1
    attempt.answers = [*attempt.answers, option]
    attempt.timings = timings
    attempt.correct += int(right)
    attempt.points += gained
    fields = ["answers", "timings", "correct", "points"]
    if len(attempt.answers) == len(attempt.questions):
        attempt.finished_at = timezone.now()
        fields.append("finished_at")
    attempt.save(update_fields=fields)
    if attempt.is_finished:
        award(attempt)
    return attempt, question, option, right


def answer_feedback(attempt, index):
    """(points earned, seconds taken) by the answer to question `index`."""
    question = attempt.questions[index]
    seconds = (attempt.timings[index] if index < len(attempt.timings) else {}).get("seconds")
    if attempt.answers[index] != question["answer"]:
        return 0, seconds
    if attempt.kind not in TIMED:
        return 1, seconds
    return speed_points(seconds if seconds is not None else 1e9), seconds


def award(attempt, notify=True):
    """Points of a finished, scored quiz: those of its answers and a bonus for 10/10."""
    if not (attempt.scored and attempt.is_finished):
        return
    when = attempt.finished_at
    if attempt.points:
        scoring.award(attempt.user, "quiz", attempt.points, attempt, when=when, notify=notify)
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
