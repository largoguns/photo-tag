from django.db import migrations


def hide_other(apps, schema_editor):
    # "Otro" is replaced by "Other…" with a name; photos that use it keep it.
    EventType = apps.get_model("catalog", "EventType")
    EventType.objects.filter(name="Otro").update(active=False)


def show_other(apps, schema_editor):
    EventType = apps.get_model("catalog", "EventType")
    EventType.objects.filter(name="Otro").update(active=True)


class Migration(migrations.Migration):
    dependencies = [("catalog", "0006_rotation_duplicates_event_proposals")]

    operations = [migrations.RunPython(hide_other, show_other)]
