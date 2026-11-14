from django.contrib.auth.decorators import login_not_required
from django.urls import include, path
from django.views.i18n import set_language

from . import views

urlpatterns = [
    path("healthz", views.healthz, name="healthz"),
    path("i18n/setlang/", login_not_required(set_language), name="set_language"),
    path("accounts/", include("accounts.urls")),
    path("admin/export/", include("exporter.urls")),
    path("", include("catalog.urls")),
]
