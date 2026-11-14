from django.urls import include, path

from . import views

urlpatterns = [
    path("healthz", views.healthz, name="healthz"),
    path("accounts/", include("accounts.urls")),
    path("admin/export/", include("exporter.urls")),
    path("", include("catalog.urls")),
]
