from django.urls import path

from gamification import views

urlpatterns = [path("", views.ranking, name="ranking")]
