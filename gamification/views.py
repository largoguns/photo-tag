from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.services.users import avatar_ids
from gamification.models import QuizAttempt
from gamification.services import pixelate, quiz, scoring

PERIODS = ("week", "month", "total")


def ranking(request):
    period = request.GET.get("period", "week")
    if period not in PERIODS:
        period = "week"
    rows = scoring.ranking(period)
    avatars = avatar_ids([user for user, _points, _level in rows])
    entries = [
        {
            "position": index + 1,
            "user": user,
            "points": points,
            "level": level,
            "avatar": avatars.get(user.pk),
            "me": user.pk == request.user.pk,
        }
        for index, (user, points, level) in enumerate(rows)
    ]
    return render(
        request,
        "gamification/ranking.html",
        {
            "period": period,
            "podium": [e for e in entries[:3] if e["points"] > 0],
            "entries": entries,
            "tab_urls": {p: f"{reverse('ranking')}?period={p}" for p in PERIODS},
        },
    )


# ---------------------------------------------------------------------------
# Family quiz (spec section 5.14)
# ---------------------------------------------------------------------------


def quiz_home(request):
    cards = [
        {
            "kind": kind,
            "label": quiz.KIND_LABELS[kind],
            "hint": quiz.KIND_HINTS[kind],
            "icon": quiz.KIND_ICONS[kind],
            "availability": quiz.availability(kind),
            "scored_left": quiz.scored_left(request.user, kind),
        }
        for kind in quiz.ORDER
    ]
    return render(
        request,
        "gamification/quiz_home.html",
        {
            "cards": cards,
            "is_player": request.user.is_player,
            "daily": quiz.DAILY_SCORED,
            "bonus": quiz.PERFECT_BONUS,
            "timed": quiz.TIMED,
        },
    )


@require_POST
def quiz_start(request):
    try:
        attempt = quiz.start(request.user, request.POST.get("kind", ""))
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
        return redirect("quiz_home")
    return redirect("quiz_play", pk=attempt.pk)


def _photo(attempt, index, question):
    """How the question's photo is shown: the URL of each level (the view refuses the
    ones whose time has not come), the level allowed now and the reveal pace."""
    if "photo_id" not in attempt.questions[index]:
        return None
    level = quiz.allowed_level(attempt, index)
    if level is None:
        return None
    timed = attempt.kind in quiz.TIMED and level < pixelate.SHARP
    return {
        "url": reverse("quiz_image", args=[attempt.pk, index, level]),
        "template": reverse("quiz_image", args=[attempt.pk, index, 0])[:-2] + "{level}/",
        "level": level,
        "levels": pixelate.SHARP,
        "timed": timed,
        "step": quiz.REVEAL_STEP,
        "elapsed": round(quiz.elapsed(attempt, index), 1),
        # Points if answered now: (until N seconds or None, points).
        "tiers": [*quiz.SPEED_POINTS, (None, 1)],
    }


def _quiz_context(attempt, **extra):
    context = {
        "attempt": attempt,
        "kind_label": quiz.KIND_LABELS[attempt.kind],
        "total": len(attempt.questions),
        "timed": attempt.kind in quiz.TIMED,
    }
    if attempt.is_finished and "question" not in extra:
        points, badges = quiz.result(attempt)
        context.update(points=points, badges=badges, perfect=attempt.correct == context["total"])
    context.update(extra)
    return context


def quiz_play(request, pk):
    """The current question (without its answer) or, once finished, the result."""
    attempt = get_object_or_404(QuizAttempt, pk=pk, user=request.user)
    if attempt.is_finished:
        return render(request, "gamification/quiz_play.html", _quiz_context(attempt))
    attempt = quiz.mark_shown(attempt)
    index = quiz.current_index(attempt)
    question = quiz.public_question(attempt.questions[index])
    return render(
        request,
        "gamification/quiz_play.html",
        _quiz_context(
            attempt, question=question, number=index + 1, photo=_photo(attempt, index, question)
        ),
    )


@require_POST
def quiz_answer(request, pk):
    """Answer the current question: the same card comes back showing right and wrong."""
    attempt = get_object_or_404(QuizAttempt, pk=pk, user=request.user)
    # Matching questions send one «match» per face asked about, in order.
    matching = "match" in request.POST
    option = request.POST.getlist("match") if matching else request.POST.get("option")
    try:
        attempt, question, chosen, right = quiz.answer(attempt, option)
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
        return redirect("quiz_play", pk=attempt.pk)
    index = len(attempt.answers) - 1
    gained, seconds = quiz.answer_feedback(attempt, index)
    matches = _matches(question, chosen)
    shown = quiz.public_question(question) | {"answer": question["answer"]}
    if matches:
        ok = {match["cover"]["number"]: match["ok"] for match in matches}
        shown["covers"] = [{**c, "ok": ok.get(c["number"])} for c in question["covers"]]
    return render(
        request,
        "gamification/quiz_play.html",
        _quiz_context(
            attempt,
            question=shown,
            number=index + 1,
            chosen=chosen,
            right=right,
            feedback=True,
            gained=gained,
            seconds=seconds,
            photo=_photo(attempt, index, question),
            matches=matches,
        ),
    )


def _matches(question, chosen):
    """For a matching question: per face asked about, its emoji, the name chosen, the
    right name and whether they agree (in the order of the faces)."""
    if not isinstance(question["answer"], list):
        return None
    asked = [cover for cover in question["covers"] if cover["number"]]
    labels = [option["label"] for option in question["options"]]
    return [
        {
            "cover": cover,
            "chosen": labels[pick],
            "right": labels[answer],
            "ok": pick == answer,
        }
        for cover, pick, answer in zip(asked, chosen, question["answer"], strict=True)
    ]


def quiz_image(request, pk, index, level):
    """The photo of a question at a pixelation level, only for the attempt's owner and
    only once that level's time has come (spec 5.14)."""
    attempt = get_object_or_404(QuizAttempt, pk=pk, user=request.user)
    allowed = quiz.allowed_level(attempt, index)
    if allowed is None or level > pixelate.SHARP:
        raise Http404
    if level > allowed:
        return HttpResponseForbidden()
    from catalog.models import Photo

    photo = get_object_or_404(Photo, pk=attempt.questions[index]["photo_id"])
    data = pixelate.render(photo, level)
    if data is None:
        raise Http404
    response = HttpResponse(data, content_type="image/jpeg")
    response["Cache-Control"] = "private, max-age=300"
    return response
