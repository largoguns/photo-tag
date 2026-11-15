"""Badges: unlocked from the points ledger (and the audit log for streaks)."""

from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Count
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from gamification.models import Achievement, ScoreEvent

IDENTITY = ("proposal.identity", "proposal.identity_suggested")


@dataclass(frozen=True)
class Badge:
    code: str
    name: str
    requirement: str
    icon: str
    reasons: tuple  # ScoreEvent reasons counted
    needed: int


BADGES = [
    Badge(
        "first_upload",
        _("Primera subida"),
        _("Sube tu primera foto"),
        "cloud_upload",
        ("photo",),
        1,
    ),
    Badge(
        "first_identity",
        _("Primera identificación"),
        _("Consigue que acepten una identidad"),
        "face",
        IDENTITY,
        1,
    ),
    Badge("detective_10", _("Detective"), _("10 identidades aceptadas"), "search", IDENTITY, 10),
    Badge(
        "detective_50", _("Gran detective"), _("50 identidades aceptadas"), "search", IDENTITY, 50
    ),
    Badge(
        "detective_100",
        _("Detective legendario"),
        _("100 identidades aceptadas"),
        "search",
        IDENTITY,
        100,
    ),
    Badge(
        "chronicler_10",
        _("Cronista"),
        _("10 fechas aceptadas"),
        "calendar_month",
        ("proposal.date",),
        10,
    ),
    Badge(
        "chronicler_50",
        _("Gran cronista"),
        _("50 fechas aceptadas"),
        "calendar_month",
        ("proposal.date",),
        50,
    ),
    Badge(
        "hawk_eye",
        _("Ojo de halcón"),
        _("5 falsos positivos aceptados"),
        "visibility",
        ("proposal.remove_face",),
        5,
    ),
    Badge("rescuer", _("Rescatista"), _("25 fotos subidas"), "photo_library", ("photo",), 25),
    Badge(
        "tiebreak",
        _("Desempate"),
        _("Tu voto decide una propuesta"),
        "how_to_vote",
        ("tiebreak",),
        1,
    ),
    Badge("streak_7", _("Racha"), _("Propón o vota 7 días seguidos"), "history", (), 7),
]
BY_CODE = {badge.code: badge for badge in BADGES}


def _streak(user):
    """Longest run of consecutive days with a proposal or a vote."""
    from accounts.models import AuditLog

    days = sorted(
        {
            timezone.localtime(moment).date()
            for moment in AuditLog.objects.filter(
                user=user, action__in=["proposal.create", "vote.cast"]
            ).values_list("created_at", flat=True)
        }
    )
    best = run = 0
    previous = None
    for day in days:
        run = run + 1 if previous and day - previous == timedelta(days=1) else 1
        best = max(best, run)
        previous = day
    return best


def _counts(user):
    return dict(
        ScoreEvent.objects.filter(user=user)
        .values("reason")
        .annotate(n=Count("pk"))
        .values_list("reason", "n")
    )


def _value(badge, counts, user):
    if badge.code == "streak_7":
        return _streak(user)
    return sum(counts.get(reason, 0) for reason in badge.reasons)


def check(user):
    """Record newly earned badges; returns their codes."""
    if not user.is_player:
        return []
    counts = _counts(user)
    owned = set(Achievement.objects.filter(user=user).values_list("code", flat=True))
    new = [
        badge.code
        for badge in BADGES
        if badge.code not in owned and _value(badge, counts, user) >= badge.needed
    ]
    Achievement.objects.bulk_create(
        [Achievement(user=user, code=code) for code in new], ignore_conflicts=True
    )
    return new


def for_user(user):
    """[(badge, unlocked_at or None, progress)] for the profile grid."""
    counts = _counts(user)
    owned = dict(Achievement.objects.filter(user=user).values_list("code", "unlocked_at"))
    return [
        (badge, owned.get(badge.code), min(_value(badge, counts, user), badge.needed))
        for badge in BADGES
    ]
