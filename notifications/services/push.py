"""Web Push delivery with VAPID (pywebpush)."""

import base64
import json
import logging

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.conf import settings
from django.utils import timezone
from pywebpush import WebPushException, webpush

logger = logging.getLogger("notifications")

TTL_SECONDS = 24 * 3600
GONE_STATUSES = {404, 410}


def is_enabled():
    return bool(settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_KEY)


def generate_vapid_keys():
    """Return (public, private) keys, base64url-encoded as browsers and pywebpush expect."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_raw = private_key.private_numbers().private_value.to_bytes(32, "big")
    public_raw = private_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )

    def b64(data):
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    return b64(public_raw), b64(private_raw)


def send(subscription, payload):
    """Deliver one notification. Returns True on success; drops expired subscriptions."""
    if not is_enabled():
        return False
    try:
        webpush(
            subscription_info=subscription.as_subscription_info(),
            data=json.dumps(payload),
            vapid_private_key=settings.VAPID_PRIVATE_KEY,
            vapid_claims={"sub": settings.VAPID_SUBJECT},
            ttl=TTL_SECONDS,
            timeout=10,
        )
    except WebPushException as exc:
        status = getattr(exc.response, "status_code", None)
        if status in GONE_STATUSES:
            logger.info("event=push_subscription_gone id=%s status=%s", subscription.pk, status)
            subscription.delete()
        else:
            logger.warning(
                "event=push_failed id=%s status=%s error=%s", subscription.pk, status, exc
            )
        return False
    except Exception:
        logger.exception("event=push_failed id=%s", subscription.pk)
        return False
    subscription.last_success_at = timezone.now()
    subscription.save(update_fields=["last_success_at"])
    return True
