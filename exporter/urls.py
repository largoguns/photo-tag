from django.urls import path

from exporter import views

urlpatterns = [
    path("", views.export_home, name="export_home"),
    path("start/", views.export_start, name="export_start"),
]
