"""Questions between users while cataloging (spec section 5.17)."""

from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from accounts.models import User
from catalog.models import Face, Photo, Question
from catalog.services import questions
from catalog.views import _run


@require_POST
def ask(request, pk):
    photo = get_object_or_404(Photo, pk=pk)
    # The topic select sends "face:<id>" for a face, or the topic itself.
    topic, _sep, face_id = request.POST.get("topic", "").partition(":")
    face = Face.objects.filter(pk=face_id).first() if face_id.isdigit() else None
    ids = [i for i in request.POST.getlist("recipients") if i.isdigit()]
    recipients = list(User.objects.filter(pk__in=ids, is_active=True))
    return _run(
        request,
        photo,
        lambda: questions.ask(
            request.user,
            photo,
            topic,
            request.POST.get("text", ""),
            face=face,
            recipients=recipients,
        ),
    )


@require_POST
def answer(request, pk):
    question = get_object_or_404(Question, pk=pk)
    return _run(
        request,
        question.photo,
        lambda: questions.answer(request.user, question, request.POST.get("text", "")),
    )


@require_POST
def resolve(request, pk):
    question = get_object_or_404(Question, pk=pk)
    action = questions.reopen if request.POST.get("reopen") else questions.resolve
    return _run(request, question.photo, lambda: action(request.user, question))


def inbox(request):
    """«Preguntas»: for me, mine or all, open or resolved."""
    box = request.GET.get("box", "for_me")
    if box not in questions.BOXES:
        box = "for_me"
    status = request.GET.get("status", Question.Status.OPEN)
    if status not in Question.Status.values:
        status = Question.Status.OPEN
    return render(
        request,
        "catalog/questions.html",
        {
            "box": box,
            "status": status,
            "items": questions.inbox(request.user, box, status)[:200],
            "counts": {
                "for_me": questions.pending_count(request.user),
            },
        },
    )
