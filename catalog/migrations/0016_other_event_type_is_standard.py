from django.db import migrations


def show_other(apps, schema_editor):
    # Event types are a standard list again: «Otro» is chosen with a description instead of
    # typing new types (which created junk). It may not exist in a fresh database.
    EventType = apps.get_model("catalog", "EventType")
    other, _created = EventType.objects.get_or_create(
        name="Otro", defaults={"name_en": "Other", "order": 999}
    )
    EventType.objects.filter(pk=other.pk).update(
        active=True, is_provisional=False, created_by=None, order=999
    )


def hide_other(apps, schema_editor):
    apps.get_model("catalog", "EventType").objects.filter(name="Otro").update(active=False)


class Migration(migrations.Migration):
    dependencies = [("catalog", "0015_direct_relationships_and_details")]
    operations = [migrations.RunPython(show_other, hide_other)]
