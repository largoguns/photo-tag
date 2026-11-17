from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from accounts.services import audit
from catalog.models import Face, Person, Photo, Proposal
from catalog.services import person_details, relationships


@transaction.atomic
def merge_persons(admin, source, target):
    """Merge `source` into `target`: faces, proposals, relationships and user link move to
    the target."""
    if source.pk == target.pk:
        raise ValidationError(_("No se puede fusionar una persona consigo misma."))
    source = Person.objects.select_for_update().get(pk=source.pk)
    target = Person.objects.select_for_update().get(pk=target.pk)
    if source.merged_into_id or target.merged_into_id:
        raise ValidationError(_("Solo se pueden fusionar personas activas."))

    photo_ids = set(Face.objects.filter(person=source).values_list("photo_id", flat=True))
    Face.objects.filter(person=source).update(person=target)
    Proposal.objects.filter(person=source).update(person=target)
    relationships.move_to(source, target)
    person_details.move_to(source, target)
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


@transaction.atomic
def rename_person(admin, person, name):
    """Give `person` a new display name (normalized). Photos are re-exported with it."""
    from catalog.services.names import name_key, normalize_name

    new = normalize_name(name)
    if not new:
        raise ValidationError(_("El nombre no puede estar vacío."))
    old = person.display_name
    if new == old:
        return person
    key = name_key(new)
    for other in Person.objects.active().exclude(pk=person.pk).only("id", "display_name"):
        if name_key(other.display_name) == key:
            raise ValidationError(
                _("Ya existe «%(name)s». Si son la misma persona, usa «Fusionar personas».")
                % {"name": other.display_name}
            )
    person.display_name = new
    person.save(update_fields=["display_name"])
    # The person's name travels in the XMP of every photo where they appear.
    photo_ids = Face.objects.filter(person=person).values_list("photo_id", flat=True)
    Photo.objects.filter(pk__in=set(photo_ids)).update(catalog_changed_at=timezone.now())
    audit.log(admin, "person.rename", person, old=old, new=new)
    return person
