"""Immediate push notification on a new level or badge (preference «Mis niveles y logros»)."""

import logging

from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext

from notifications.models import NotificationSettings, PushSubscription
from notifications.services import push

logger = logging.getLogger("gamification")

TAG = "phototag-progress"


def _send(user, make_body):
    if not push.is_enabled() or not NotificationSettings.for_user(user).achievements:
        return 0
    sent = 0
    for subscription in PushSubscription.objects.filter(user=user):
        try:
            with translation.override(subscription.language):
                payload = {
                    "title": "PhotoTag",
                    "body": make_body(),
                    "url": reverse("profile") + "#setting-progress",
                    "tag": TAG,
                }
            sent += push.send(subscription, payload)
        except Exception:  # a notification never breaks scoring
            logger.exception("event=progress_notification_failed user=%s", user.pk)
    return sent


def level_up(user, level):
    return _send(
        user,
        lambda: gettext("¡Has subido de nivel! Ahora eres %(level)s.") % {"level": level.name},
    )


def achievement(user, code):
    from gamification.services.achievements import BY_CODE

    badge = BY_CODE[code]
    return _send(
        user,
        lambda: (
            gettext("Nuevo logro: %(name)s · %(what)s")
            % {
                "name": badge.name,
                "what": badge.requirement,
            }
        ),
    )
