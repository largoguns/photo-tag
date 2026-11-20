from django.db import migrations


def to_review(apps, schema_editor):
    """New event types go through an administrator again (spec 5.5).

    «Otro» stops being a standard type (it is chosen as «Otro…» with a name), and the types
    the family typed before are put back into review, so they stop being offered until an
    administrator accepts, rewrites or rejects them. Their photos keep them meanwhile.
    """
    EventType = apps.get_model("catalog", "EventType")
    EventType.objects.filter(name="Otro").update(active=False)
    EventType.objects.filter(created_by__isnull=False).update(is_provisional=True)


def back(apps, schema_editor):
    EventType = apps.get_model("catalog", "EventType")
    EventType.objects.filter(name="Otro").update(active=True)
    EventType.objects.filter(created_by__isnull=False, photos__isnull=False).update(
        is_provisional=False
    )


class Migration(migrations.Migration):
    dependencies = [("catalog", "0016_other_event_type_is_standard")]
    operations = [migrations.RunPython(to_review, back)]
