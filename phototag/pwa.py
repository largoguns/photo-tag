"""Progressive Web App endpoints: manifest, service worker and offline page.

All three are public (no session): browsers fetch them without cookies, and they
contain no private data.
"""

import hashlib

from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.http import JsonResponse
from django.shortcuts import render
from django.templatetags.static import static
from django.utils.translation import get_language
from django.utils.translation import gettext as _
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_GET

THEME_COLOR = "#1f5fbf"
BACKGROUND_COLOR = "#f6f5f2"
ICONS = [
    ("img/icons/icon-192.png", "192x192", "any"),
    ("img/icons/icon-512.png", "512x512", "any"),
    ("img/icons/icon-maskable-512.png", "512x512", "maskable"),
]
# Static files every page needs; precached so the app shell loads offline.
SHELL_STATIC = [
    "css/app.css",
    "js/app.js",
    "js/htmx.min.js",
    "img/icons/icon-192.png",
    "img/icons/favicon-32.png",
]


@login_not_required
@require_GET
@cache_control(max_age=3600)
def manifest(request):
    data = {
        "id": "/",
        "name": "PhotoTag",
        "short_name": "PhotoTag",
        "description": _("Catalogación colaborativa de las fotos familiares"),
        "lang": get_language(),
        "start_url": "/?source=pwa",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "theme_color": THEME_COLOR,
        "background_color": BACKGROUND_COLOR,
        "icons": [
            {"src": static(path), "sizes": sizes, "type": "image/png", "purpose": purpose}
            for path, sizes, purpose in ICONS
        ],
    }
    return JsonResponse(
        data,
        content_type="application/manifest+json",
        json_dumps_params={"ensure_ascii": False},
    )


@login_not_required
@require_GET
@cache_control(no_cache=True)
def service_worker(request):
    shell = [static(path) for path in SHELL_STATIC]
    return render(
        request,
        "pwa/sw.js",
        {
            "shell_urls": shell,
            # The cache name changes whenever a hashed static file changes.
            "cache_version": hashlib.sha256("\n".join(shell).encode()).hexdigest()[:12],
            "offline_url": "/offline/",
            "static_prefix": settings.STATIC_URL,
        },
        content_type="application/javascript",
    )


@login_not_required
@require_GET
def offline(request):
    return render(request, "pwa/offline.html")
