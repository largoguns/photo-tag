from django.urls import path

from notifications import views

urlpatterns = [
    path("subscribe/", views.subscribe, name="push_subscribe"),
    path("unsubscribe/", views.unsubscribe, name="push_unsubscribe"),
    path("test/", views.test, name="push_test"),
]
