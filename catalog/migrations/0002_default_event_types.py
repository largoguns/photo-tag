from django.db import migrations

DEFAULT_EVENT_TYPES = [
    "Cumpleaños",
    "Boda",
    "Comunión",
    "Bautizo",
    "Navidad",
    "Vacaciones",
    "Otro",
]


def create_event_types(apps, schema_editor):
    EventType = apps.get_model("catalog", "EventType")
    for order, name in enumerate(DEFAULT_EVENT_TYPES):
        EventType.objects.get_or_create(name=name, defaults={"order": order * 10})


class Migration(migrations.Migration):
    dependencies = [("catalog", "0001_initial")]

    operations = [migrations.RunPython(create_event_types, migrations.RunPython.noop)]
