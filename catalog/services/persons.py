from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from accounts.services import audit
from catalog.models import Face, Person, Photo, Proposal


@transaction.atomic
def merge_persons(admin, source, target):
    """Merge `source` into `target`: faces, proposals and user link move to the target."""
    if source.pk == target.pk:
        raise ValidationError(_("No se puede fusionar una persona consigo misma."))
    source = Person.objects.select_for_update().get(pk=source.pk)
    target = Person.objects.select_for_update().get(pk=target.pk)
    if source.merged_into_id or target.merged_into_id:
        raise ValidationError(_("Solo se pueden fusionar personas activas."))

    photo_ids = set(Face.objects.filter(person=source).values_list("photo_id", flat=True))
    Face.objects.filter(person=source).update(person=target)
    Proposal.objects.filter(person=source).update(person=target)
    Photo.objects.filter(pk__in=photo_ids).update(catalog_changed_at=timezone.now())

    if source.user_id and not target.user_id:
        target.user_id = source.user_id
        source.user = None
        source.save(update_fields=["user"])
    if source.notes and source.notes not in target.notes:
        target.notes = "\n".join(filter(None, [target.notes, source.notes]))
    target.is_provisional = False
    target.save()

    source.merged_into = target
    source.user = None
    source.save(update_fields=["merged_into", "user"])
    audit.log(admin, "person.merge", target, source_id=source.pk, source_name=source.display_name)
    return target


def photo_count(person):
    return (
        Photo.objects.visible()
        .filter(faces__person=person, faces__status=Face.Status.ACTIVE)
        .distinct()
        .count()
    )
