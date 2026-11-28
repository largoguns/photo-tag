"""Questions between users while cataloging a photo (spec section 5.17).

Not a chat: a question has a topic (a face, the event, the date, the location or general),
goes to everyone or to some users, gets answers in a thread and is resolved by its author
or an administrator. An answer to a resolved question opens it again.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone
from django.utils.translation import gettext as _

from accounts.services import audit
from catalog.models import Answer, Question

Topic = Question.Topic
Status = Question.Status
MAX_TEXT = 1000
BOXES = ("for_me", "mine", "all")


def _clean(text):
    text = str(text or "").strip()
    if not text:
        raise ValidationError(_("Escribe la pregunta."))
    if len(text) > MAX_TEXT:
        raise ValidationError(_("Como mucho %(count)s caracteres.") % {"count": MAX_TEXT})
    return text


def can_resolve(user, question):
    return question.author_id == user.pk or user.is_catalog_admin


@transaction.atomic
def ask(user, photo, topic, text, face=None, recipients=()):
    """New open question about `photo`; `recipients` empty means everyone."""
    if topic not in Topic.values:
        raise ValidationError(_("Elige sobre qué es la pregunta."))
    if topic == Topic.FACE:
        if face is None or face.photo_id != photo.pk or not face.is_visible:
            raise ValidationError(_("Elige una cara de esta foto."))
    else:
        face = None
    question = Question.objects.create(
        photo=photo, face=face, topic=topic, text=_clean(text), author=user
    )
    recipients = [r for r in recipients if r.pk != user.pk and r.is_active]
    if recipients:
        question.recipients.set(recipients)
    audit.log(
        user,
        "question.create",
        photo,
        question=question.pk,
        topic=topic,
        recipients=[r.pk for r in recipients],
    )
    return question


@transaction.atomic
def answer(user, question, text):
    """Add an answer; a resolved question is opened again (there is something new)."""
    question = Question.objects.select_for_update().get(pk=question.pk)
    reply = Answer.objects.create(question=question, author=user, text=_clean(text))
    question.updated_at = timezone.now()
    fields = ["updated_at"]
    if not question.is_open:
        question.status, question.resolved_by, question.resolved_at = Status.OPEN, None, None
        fields += ["status", "resolved_by", "resolved_at"]
    question.save(update_fields=fields)
    audit.log(user, "question.answer", question.photo, question=question.pk)
    return reply


@transaction.atomic
def resolve(user, question):
    question = Question.objects.select_for_update().get(pk=question.pk)
    if not can_resolve(user, question):
        raise PermissionDenied
    if question.is_open:
        now = timezone.now()
        question.status, question.resolved_by, question.resolved_at = Status.RESOLVED, user, now
        question.updated_at = now
        question.save(update_fields=["status", "resolved_by", "resolved_at", "updated_at"])
        audit.log(user, "question.resolve", question.photo, question=question.pk)
    return question


@transaction.atomic
def reopen(user, question):
    question = Question.objects.select_for_update().get(pk=question.pk)
    if not can_resolve(user, question):
        raise PermissionDenied
    if not question.is_open:
        question.status, question.resolved_by, question.resolved_at = Status.OPEN, None, None
        question.updated_at = timezone.now()
        question.save(update_fields=["status", "resolved_by", "resolved_at", "updated_at"])
        audit.log(user, "question.reopen", question.photo, question=question.pk)
    return question


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _with_details(questions):
    return questions.select_related("photo", "face", "author", "resolved_by").annotate(
        answer_count=Count("answers", distinct=True)
    )


def for_photo(photo):
    """The photo's questions, open first, with their answers and recipients."""
    return list(
        _with_details(photo.questions.all())
        .prefetch_related("recipients", "answers__author")
        .order_by("status", "-updated_at")  # "open" < "resolved": open ones first
    )


def open_photo_ids():
    return set(Question.objects.filter(status=Status.OPEN).values_list("photo_id", flat=True))


def _for_me(user):
    """Open questions waiting for `user`: addressed to them, or to everyone (not their
    own) while they have not answered yet."""
    answered = Answer.objects.filter(question=OuterRef("pk"), author=user)
    return (
        Question.objects.filter(status=Status.OPEN)
        .exclude(author=user)
        .annotate(
            has_recipients=Exists(
                Question.recipients.through.objects.filter(question_id=OuterRef("pk"))
            )
        )
        .filter(Q(recipients=user) | (Q(has_recipients=False) & ~Exists(answered)))
        .distinct()
    )


def inbox(user, box="for_me", status=Status.OPEN):
    if box not in BOXES:
        box = "for_me"
    if box == "for_me":
        questions = _for_me(user)
    elif box == "mine":
        questions = Question.objects.filter(author=user)
    else:
        questions = Question.objects.all()
    if box != "for_me" and status in Status.values:
        questions = questions.filter(status=status)
    return _with_details(questions).prefetch_related("recipients").order_by("-updated_at")


def pending_count(user):
    """How many questions wait for `user` (avatar menu, home card)."""
    if not user.is_authenticated:
        return 0
    return _for_me(user).count()


def new_for(user, since):
    """Questions for `user` asked since `since` (push digest)."""
    return _for_me(user).filter(created_at__gt=since).count()


def answers_for(user, since):
    """Answers by others to `user`'s questions since `since` (push digest)."""
    return (
        Answer.objects.filter(question__author=user, created_at__gt=since)
        .exclude(author=user)
        .count()
    )
