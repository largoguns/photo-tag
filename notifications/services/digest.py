"""Grouped push notifications (spec section 5.9).

No event queue: each pass counts what happened since the last notification of each
kind, sends at most one notification per kind and interval, and stays quiet at night.
"""

import datetime
import logging
from dataclasses import dataclass

from django.conf import settings
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.translation import gettext, ngettext

from catalog.models import Photo, Proposal
from catalog.services.voting import pending_for_user
from notifications.models import NotificationSettings, PushSubscription
from notifications.services import push

logger = logging.getLogger("notifications")


@dataclass
class Digest:
    kind: str  # "new_photos" | "pending_votes" | "resolved" | "challenge"
    counts: dict

    @property
    def tag(self):
        return f"phototag-{self.kind}"

    def url(self):
        if self.kind == "challenge":
            return {
                "overtaken": reverse("ranking"),
                "votes": reverse("gallery") + "?mine=1",
            }.get(self.counts["variant"], reverse("next_uncataloged"))
        if self.kind == "new_photos":
            return reverse("next_uncataloged")
        if self.kind == "pending_votes":
            return reverse("gallery") + "?mine=1"
        return reverse("home")

    def body(self):
        """Generic text in the active language; never includes people's names."""
        if self.kind == "challenge":
            return challenge_body(self.counts)
        if self.kind == "new_photos":
            n = self.counts["photos"]
            return ngettext(
                "%(count)s foto nueva para catalogar",
                "%(count)s fotos nuevas para catalogar",
                n,
            ) % {"count": n}
        if self.kind == "pending_votes":
            n = self.counts["proposals"]
            return ngettext(
                "%(count)s propuesta espera tu voto",
                "%(count)s propuestas esperan tu voto",
                n,
            ) % {"count": n}
        parts = []
        if self.counts["accepted"]:
            n = self.counts["accepted"]
            parts.append(
                ngettext(
                    "%(count)s propuesta tuya aceptada",
                    "%(count)s propuestas tuyas aceptadas",
                    n,
                )
                % {"count": n}
            )
        if self.counts["rejected"]:
            n = self.counts["rejected"]
            parts.append(
                ngettext(
                    "%(count)s propuesta tuya rechazada",
                    "%(count)s propuestas tuyas rechazadas",
                    n,
                )
                % {"count": n}
            )
        return " · ".join(parts)

    def payload(self):
        return {"title": "PhotoTag", "body": self.body(), "url": self.url(), "tag": self.tag}


def challenge_body(counts):
    from gamification.services.levels import level_for

    if counts["variant"] == "overtaken":
        return ngettext(
            "Te han adelantado en el ranking de la semana: "
            "estás a %(gap)s punto del puesto %(place)s.",
            "Te han adelantado en el ranking de la semana: "
            "estás a %(gap)s puntos del puesto %(place)s.",
            counts["gap"],
        ) % {"gap": counts["gap"], "place": counts["place"]}
    level = level_for(counts["points"])
    if level.next_name:
        goal = gettext("Te faltan %(missing)s puntos para %(next)s") % {
            "missing": level.missing,
            "next": level.next_name,
        }
    else:
        goal = gettext("Sigue cuidando el álbum")
    if counts["variant"] == "votes":
        task = ngettext(
            "hay %(count)s propuesta esperando tu voto",
            "hay %(count)s propuestas esperando tu voto",
            counts["count"],
        )
    else:
        task = ngettext(
            "hay %(count)s foto por catalogar",
            "hay %(count)s fotos por catalogar",
            counts["count"],
        )
    return f"{goal}: {task % {'count': counts['count']}}."


def challenge_for(user, now):
    """The most motivating challenge for an idle collaborator, or None."""
    from accounts.models import AuditLog
    from gamification.services import scoring

    if not user.is_player:
        return None
    recent = now - datetime.timedelta(days=2)
    if AuditLog.objects.filter(
        user=user, action__in=["proposal.create", "vote.cast"], created_at__gte=recent
    ).exists():
        return None  # already playing: no need to nudge
    rows = scoring.ranking("week")
    position = next((i for i, row in enumerate(rows) if row[0].pk == user.pk), None)
    if position:  # someone is ahead this week
        mine, ahead = rows[position][1], rows[position - 1][1]
        if ahead > mine:
            return {"variant": "overtaken", "gap": ahead - mine, "place": position}
    points = scoring.total(user)
    votes = pending_for_user(user).exclude(author=user).count()
    if votes:
        return {"variant": "votes", "count": votes, "points": points}
    photos = Photo.objects.visible().exclude(status=Photo.Status.CATALOGED).count()
    if photos:
        return {"variant": "photos", "count": photos, "points": points}
    return None


def parse_quiet_hours(value):
    """'22-9' → (22, 9). Empty or invalid disables quiet hours."""
    try:
        start, end = (int(part) for part in str(value).split("-"))
    except ValueError:
        return None
    if not (0 <= start <= 23 and 0 <= end <= 23) or start == end:
        return None
    return start, end


def in_quiet_hours(now, quiet=None):
    quiet = parse_quiet_hours(settings.NOTIFY_QUIET_HOURS if quiet is None else quiet)
    if quiet is None:
        return False
    start, end = quiet
    hour = timezone.localtime(now).hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # crosses midnight, e.g. 22-9


def collect(user, prefs, now=None):
    """Digests with something new since the last notification of each kind."""
    digests = []
    if prefs.challenges:
        challenge = challenge_for(user, now or timezone.now())
        if challenge:
            digests.append(Digest("challenge", challenge))
    if prefs.new_photos:
        photos = Photo.objects.visible().filter(created_at__gt=prefs.new_photos_sent_at).count()
        if photos:
            digests.append(Digest("new_photos", {"photos": photos}))
    if prefs.pending_votes:
        proposals = (
            pending_for_user(user)
            .filter(created_at__gt=prefs.pending_votes_sent_at)
            .exclude(author=user)
            .count()
        )
        if proposals:
            digests.append(Digest("pending_votes", {"proposals": proposals}))
    if prefs.resolved:
        decided = Proposal.objects.filter(author=user, decided_at__gt=prefs.resolved_sent_at)
        accepted = decided.filter(status=Proposal.Status.ACCEPTED).count()
        rejected = decided.filter(status=Proposal.Status.REJECTED).count()
        if accepted or rejected:
            digests.append(Digest("resolved", {"accepted": accepted, "rejected": rejected}))
    return digests


def deliver(user, digest):
    """Send one digest to every device of the user, each in its own language."""
    delivered = False
    for subscription in list(PushSubscription.objects.filter(user=user)):
        with translation.override(subscription.language):
            payload = digest.payload()
        delivered = push.send(subscription, payload) or delivered
    return delivered


def send_digests(now=None):
    """Worker job. Returns how many notifications were delivered."""
    if not push.is_enabled():
        return 0
    now = now or timezone.now()
    if in_quiet_hours(now):
        return 0
    interval = datetime.timedelta(minutes=settings.NOTIFY_MIN_INTERVAL_MINUTES)
    challenge_interval = datetime.timedelta(days=settings.NOTIFY_CHALLENGE_DAYS)
    users = (
        get_user_model()
        .objects.filter(is_active=True, must_change_password=False)
        .filter(push_subscriptions__isnull=False)
        .distinct()
    )
    sent = 0
    for user in users:
        prefs = NotificationSettings.for_user(user)
        for digest in collect(user, prefs, now):
            field = f"{digest.kind}_sent_at"
            wait = challenge_interval if digest.kind == "challenge" else interval
            if now - getattr(prefs, field) < wait:
                continue
            if deliver(user, digest):
                setattr(prefs, field, now)
                prefs.save(update_fields=[field])
                sent += 1
    if sent:
        logger.info("event=digests_sent count=%d", sent)
    return sent


def send_test(user):
    """Immediate notification to check that a user's devices receive pushes."""
    delivered = 0
    for subscription in list(PushSubscription.objects.filter(user=user)):
        with translation.override(subscription.language):
            payload = {
                "title": "PhotoTag",
                "body": gettext("Los avisos funcionan en este dispositivo."),
                "url": reverse("home"),
                "tag": "phototag-test",
            }
        delivered += push.send(subscription, payload)
    return delivered
