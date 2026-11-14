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
    kind: str  # "new_photos" | "pending_votes" | "resolved"
    counts: dict

    @property
    def tag(self):
        return f"phototag-{self.kind}"

    def url(self):
        if self.kind == "new_photos":
            return reverse("next_uncataloged")
        if self.kind == "pending_votes":
            return reverse("gallery") + "?mine=1"
        return reverse("my_activity")

    def body(self):
        """Generic text in the active language; never includes people's names."""
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


def collect(user, prefs):
    """Digests with something new since the last notification of each kind."""
    digests = []
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
    users = (
        get_user_model()
        .objects.filter(is_active=True, must_change_password=False)
        .filter(push_subscriptions__isnull=False)
        .distinct()
    )
    sent = 0
    for user in users:
        prefs = NotificationSettings.for_user(user)
        for digest in collect(user, prefs):
            field = f"{digest.kind}_sent_at"
            if now - getattr(prefs, field) < interval:
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
