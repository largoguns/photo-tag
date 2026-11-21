from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.services.users import avatar_ids
from gamification.models import QuizAttempt
from gamification.services import quiz, scoring

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
        for kind in quiz.Kind.values
    ]
    return render(
        request,
        "gamification/quiz_home.html",
        {
            "cards": cards,
            "is_player": request.user.is_player,
            "daily": quiz.DAILY_SCORED,
            "bonus": quiz.PERFECT_BONUS,
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


def _quiz_context(attempt, **extra):
    context = {
        "attempt": attempt,
        "kind_label": quiz.KIND_LABELS[attempt.kind],
        "total": len(attempt.questions),
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
    index = quiz.current_index(attempt)
    question = {k: v for k, v in attempt.questions[index].items() if k != "answer"}
    return render(
        request,
        "gamification/quiz_play.html",
        _quiz_context(attempt, question=question, number=index + 1),
    )


@require_POST
def quiz_answer(request, pk):
    """Answer the current question: the same card comes back showing right and wrong."""
    attempt = get_object_or_404(QuizAttempt, pk=pk, user=request.user)
    try:
        attempt, question, chosen, right = quiz.answer(attempt, request.POST.get("option"))
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
        return redirect("quiz_play", pk=attempt.pk)
    return render(
        request,
        "gamification/quiz_play.html",
        _quiz_context(
            attempt,
            question=question,
            number=len(attempt.answers),
            chosen=chosen,
            right=right,
            feedback=True,
        ),
    )
