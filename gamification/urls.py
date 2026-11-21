from django.urls import path

from gamification import views

urlpatterns = [
    path("", views.ranking, name="ranking"),
    path("quiz/", views.quiz_home, name="quiz_home"),
    path("quiz/start/", views.quiz_start, name="quiz_start"),
    path("quiz/<int:pk>/", views.quiz_play, name="quiz_play"),
    path("quiz/<int:pk>/answer/", views.quiz_answer, name="quiz_answer"),
]
