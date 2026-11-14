import json
import re

from django.http import HttpResponseBadRequest, JsonResponse
from django.utils.translation import get_language
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from accounts.services import audit
from notifications.models import PushSubscription
from notifications.services import digest, push

MAX_BODY_BYTES = 4096


def device_label(user_agent):
    """Short human label such as 'Chrome · Android' from a user agent."""
    ua = user_agent or ""
    browser = next(
        (
            name
            for pattern, name in [
                (r"EdgA?/", "Edge"),
                (r"SamsungBrowser/", "Samsung Internet"),
                (r"Firefox/|FxiOS/", "Firefox"),
                (r"Chrome/|CriOS/", "Chrome"),
                (r"Safari/", "Safari"),
            ]
            if re.search(pattern, ua)
        ),
        "",
    )
    system = next(
        (
            name
            for pattern, name in [
                (r"iPhone|iPad|iPod", "iOS"),
                (r"Android", "Android"),
                (r"Windows", "Windows"),
                (r"Mac OS X|Macintosh", "macOS"),
                (r"Linux", "Linux"),
            ]
            if re.search(pattern, ua)
        ),
        "",
    )
    return " · ".join(filter(None, [browser, system]))[:100]


def _json_body(request):
    if len(request.body) > MAX_BODY_BYTES:
        return None
    try:
        return json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        return None


@require_POST
def subscribe(request):
    if not push.is_enabled():
        return JsonResponse({"error": "disabled"}, status=404)
    data = _json_body(request)
    try:
        endpoint = data["endpoint"]
        keys = data["keys"]
        p256dh, auth = str(keys["p256dh"]), str(keys["auth"])
    except (KeyError, TypeError):
        return HttpResponseBadRequest("invalid subscription")
    if not str(endpoint).startswith("https://") or len(endpoint) > 1000:
        return HttpResponseBadRequest("invalid endpoint")
    subscription, created = PushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={
            "user": request.user,
            "p256dh": p256dh[:200],
            "auth": auth[:100],
            "language": get_language() or "es",
            "device_label": device_label(request.headers.get("User-Agent")),
        },
    )
    if created:
        audit.log(request.user, "push.subscribe", subscription, device=subscription.device_label)
    return JsonResponse({"ok": True, "device": subscription.device_label})


@require_POST
def unsubscribe(request):
    data = _json_body(request) or {}
    deleted, _rows = PushSubscription.objects.filter(
        user=request.user, endpoint=data.get("endpoint", "")
    ).delete()
    if deleted:
        audit.log(request.user, "push.unsubscribe")
    return JsonResponse({"ok": True, "deleted": deleted})


@require_POST
def test(request):
    delivered = digest.send_test(request.user)
    if delivered:
        message = _("Aviso de prueba enviado.")
    else:
        message = _("No se pudo enviar el aviso. Activa los avisos en este dispositivo.")
    return JsonResponse({"ok": bool(delivered), "message": message})
