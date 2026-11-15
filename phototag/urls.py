from django.contrib.auth.decorators import login_not_required
from django.urls import include, path
from django.views.i18n import set_language

from . import pwa, views

urlpatterns = [
    path("healthz", views.healthz, name="healthz"),
    path("manifest.webmanifest", pwa.manifest, name="pwa_manifest"),
    path("sw.js", pwa.service_worker, name="pwa_service_worker"),
    path("offline/", pwa.offline, name="pwa_offline"),
    path("i18n/setlang/", login_not_required(set_language), name="set_language"),
    path("accounts/", include("accounts.urls")),
    path("admin/export/", include("exporter.urls")),
    path("notifications/", include("notifications.urls")),
    path("upload/", include("scanner.urls")),
    path("", include("catalog.urls")),
]
