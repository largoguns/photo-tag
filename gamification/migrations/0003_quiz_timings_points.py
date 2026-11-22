from django.db import migrations, models
from django.db.models import F


def points_from_correct(apps, schema_editor):
    """Quizzes played before timed kinds existed earned 1 point per right answer."""
    QuizAttempt = apps.get_model("gamification", "QuizAttempt")
    QuizAttempt.objects.update(points=F("correct"))


class Migration(migrations.Migration):
    dependencies = [
        ("gamification", "0002_quiz_attempt"),
    ]

    operations = [
        migrations.AddField(
            model_name="quizattempt",
            name="timings",
            field=models.JSONField(default=list),
        ),
        migrations.AddField(
            model_name="quizattempt",
            name="points",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.RunPython(points_from_correct, migrations.RunPython.noop),
    ]
