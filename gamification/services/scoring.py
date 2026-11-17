"""Points for collaborators: awarded for results (accepted proposals, right votes, photos).

Only collaborators play (`User.is_player`). Hooks in voting, the pipeline and deletions
call these functions; they never let a scoring failure break the action that triggered it.
"""

import logging
from datetime import timedelta
from functools import wraps

from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from gamification.models import ScoreEvent
from gamification.services.levels import level_for

logger = logging.getLogger("gamification")

PHOTO_POINTS = 5
VOTE_POINTS = 2
VOTE_DAILY_CAP = 20
FIRST_IDENTITY_POINTS = 15
PROPOSAL_POINTS = {
    "face_identity": ("proposal.identity", 20),
    "event": ("proposal.event", 10),
    "date": ("proposal.date", 10),
    "location": ("proposal.location", 10),
    "add_face": ("proposal.add_face", 15),
    "remove_face": ("proposal.remove_face", 5),
}
SUGGESTED_IDENTITY = ("proposal.identity_suggested", 10)

REASONS = {
    "photo": _("Foto subida"),
    "proposal.identity": _("Identidad aceptada"),
    "proposal.identity_suggested": _("Sugerencia confirmada"),
    "proposal.event": _("Evento aceptado"),
    "proposal.date": _("Fecha aceptada"),
    "proposal.location": _("Ubicación aceptada"),
    "proposal.add_face": _("Cara nueva aceptada"),
    "proposal.remove_face": _("Falso positivo aceptado"),
    "first_identity": _("Primera identificación de una persona"),
    "vote": _("Voto acertado"),
    "tiebreak": _("Desempate"),
}


def safe(function):
    """Scoring must never break voting, processing or deleting."""

    @wraps(function)
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            logger.exception("event=scoring_failed function=%s", function.__name__)
            return None

    return wrapper


def _ref(obj):
    return obj._meta.label_lower, str(obj.pk)


def total(user, since=None):
    events = ScoreEvent.objects.filter(user=user)
    if since is not None:
        events = events.filter(created_at__gte=since)
    return events.aggregate(points=Sum("points"))["points"] or 0


def award(user, reason, points, obj, when=None, notify=True):
    """Give points once per (user, reason, object). Returns True if it was new."""
    if user is None or not user.is_player:
        return False
    ref_type, ref_id = _ref(obj)
    before = total(user) if notify else 0
    _event, created = ScoreEvent.objects.get_or_create(
        user=user,
        reason=reason,
        ref_type=ref_type,
        ref_id=ref_id,
        defaults={"points": points, "created_at": when or timezone.now()},
    )
    if created:
        _progress_changed(user, before, notify)
    return created


def revoke(obj, reasons=None):
    ref_type, ref_id = _ref(obj)
    events = ScoreEvent.objects.filter(ref_type=ref_type, ref_id=ref_id)
    if reasons:
        events = events.filter(reason__in=reasons)
    events.delete()


def _progress_changed(user, before, notify):
    from gamification.services import achievements, announcements

    after = total(user)
    new_badges = achievements.check(user)
    if not notify:
        return
    if level_for(after).number > level_for(before).number:
        announcements.level_up(user, level_for(after))
    for code in new_badges:
        announcements.achievement(user, code)


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


def _vote_points_today(user, when):
    start = timezone.localtime(when).replace(hour=0, minute=0, second=0, microsecond=0)
    events = ScoreEvent.objects.filter(
        user=user, reason="vote", created_at__gte=start, created_at__lt=start + timedelta(days=1)
    )
    return events.aggregate(points=Sum("points"))["points"] or 0


@safe
def proposal_closed(proposal, previous_status, notify=True):
    """A proposal was accepted, rejected or superseded."""
    from catalog.models import Proposal, Vote

    Status = Proposal.Status
    when = proposal.decided_at or timezone.now()
    if proposal.status == Status.SUPERSEDED and previous_status == Status.ACCEPTED:
        # Someone corrected it later: it was not right after all.
        revoke(proposal, [r for r, _p in PROPOSAL_POINTS.values()] + [SUGGESTED_IDENTITY[0]])
        return
    if proposal.status not in (Status.ACCEPTED, Status.REJECTED):
        return

    author = proposal.author
    if proposal.status == Status.ACCEPTED:
        reason, points = PROPOSAL_POINTS[proposal.kind]
        if proposal.kind == Proposal.Kind.FACE_IDENTITY and proposal.value.get("suggested"):
            reason, points = SUGGESTED_IDENTITY
        award(author, reason, points, proposal, when, notify)
        if proposal.kind == Proposal.Kind.FACE_IDENTITY and proposal.person_id:
            person = proposal.person.resolved()
            first = not ScoreEvent.objects.filter(
                reason="first_identity", ref_type="catalog.person", ref_id=str(person.pk)
            ).exists()
            others = Proposal.objects.filter(
                kind=Proposal.Kind.FACE_IDENTITY, status=Status.ACCEPTED, person=person
            ).exclude(pk=proposal.pk)
            if first and not others.filter(decided_at__lt=when).exists():
                award(author, "first_identity", FIRST_IDENTITY_POINTS, person, when, notify)

    right = Vote.UP if proposal.status == Status.ACCEPTED else Vote.DOWN
    votes = (
        Vote.objects.filter(proposal=proposal, value=right)
        .exclude(user_id=proposal.author_id)
        .select_related("user")
    )
    for vote in votes:
        if vote.user.is_player and _vote_points_today(vote.user, when) < VOTE_DAILY_CAP:
            award(vote.user, "vote", VOTE_POINTS, vote, when, notify)


@safe
def vote_decided(vote, notify=True):
    """The vote that closed a proposal: the «Desempate» badge (no points)."""
    if vote.user_id != vote.proposal.author_id:
        award(vote.user, "tiebreak", 0, vote, notify=notify)


@safe
def photo_added(photo, notify=True):
    if photo.discarded or photo.duplicate_of_id:
        return
    user = photo.scan.uploaded_by
    award(user, "photo", PHOTO_POINTS, photo, photo.created_at, notify)


@safe
def photo_removed(photo, deleted=False):
    """Discarded (reversible): its photo points go. Deleted: so do its proposals' and votes'."""
    from catalog.models import Proposal, Vote

    revoke(photo, ["photo"])
    if not deleted:
        return
    for proposal in Proposal.objects.filter(photo=photo):
        revoke(proposal)
    for vote in Vote.objects.filter(proposal__photo=photo):
        revoke(vote)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def progress(user):
    """Points, level, accuracy and badges of a collaborator (profile, home)."""
    from catalog.models import Proposal
    from gamification.services import achievements

    points = total(user)
    closed = Proposal.objects.filter(
        author=user, status__in=[Proposal.Status.ACCEPTED, Proposal.Status.REJECTED]
    )
    accepted = closed.filter(status=Proposal.Status.ACCEPTED).count()
    count = closed.count()
    return {
        "points": points,
        "level": level_for(points),
        "accuracy": round(100 * accepted / count) if count else None,
        "badges": achievements.for_user(user),
    }


def period_start(period, now=None):
    now = timezone.localtime(now or timezone.now())
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        return day - timedelta(days=day.weekday())
    if period == "month":
        return day.replace(day=1)
    return None


def ranking(period="total"):
    """[(user, points, level)] of every active collaborator, best first."""
    from accounts.models import Role, User

    since = period_start(period)
    events = ScoreEvent.objects.all()
    if since is not None:
        events = events.filter(created_at__gte=since)
    points = dict(events.values("user").annotate(sum=Sum("points")).values_list("user", "sum"))
    totals = dict(
        ScoreEvent.objects.values("user").annotate(sum=Sum("points")).values_list("user", "sum")
    )
    players = User.objects.filter(role=Role.COLLABORATOR, is_active=True)
    rows = [
        (player, points.get(player.pk, 0) or 0, level_for(totals.get(player.pk, 0) or 0))
        for player in players
    ]
    rows.sort(key=lambda row: (-row[1], row[0].get_display_name().casefold()))
    return rows


# ---------------------------------------------------------------------------
# Rebuild from history
# ---------------------------------------------------------------------------


def rebuild():
    """Recompute every point from what already exists (no notifications)."""
    from catalog.models import Photo, Proposal
    from gamification.models import Achievement
    from gamification.services import achievements

    ScoreEvent.objects.all().delete()
    Achievement.objects.all().delete()
    for photo in Photo.objects.select_related("scan__uploaded_by").order_by("created_at", "pk"):
        photo_added(photo, notify=False)
    closed = Proposal.objects.filter(
        status__in=[Proposal.Status.ACCEPTED, Proposal.Status.REJECTED]
    ).select_related("author", "person")
    for proposal in closed.order_by("decided_at", "pk"):
        proposal_closed(proposal, None, notify=False)
    from accounts.models import Role, User

    for user in User.objects.filter(role=Role.COLLABORATOR):
        achievements.check(user)
    return ScoreEvent.objects.count()
