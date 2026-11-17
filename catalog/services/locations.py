"""Location catalogue administration (spec section 5.5, screen 8): rename, merge, (de)activate.

Locations are created by proposals (voting.resolve_location); administrators curate them.
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from accounts.services import audit
from catalog.models import Location, Photo, Proposal
from catalog.services.names import name_key


def _mark_photos_changed(location):
    """Photos carry the location name in their XMP: export them again."""
    Photo.objects.filter(location=location).update(catalog_changed_at=timezone.now())


@transaction.atomic
def rename_location(admin, location, name):
    text = " ".join(str(name).split())[:150]
    if not text:
        raise ValidationError(_("El nombre no puede estar vacío."))
    old = location.name
    if text == old:
        return location
    key = name_key(text)
    other = Location.objects.filter(name_key=key).exclude(pk=location.pk).first()
    if other:
        raise ValidationError(
            _("Ya existe «%(name)s». Si son el mismo sitio, usa «Fusionar».") % {"name": other.name}
        )
    location.name, location.name_key = text, key
    location.save(update_fields=["name", "name_key"])
    _mark_photos_changed(location)
    audit.log(admin, "location.rename", location, old=old, new=text)
    return location


@transaction.atomic
def set_active(admin, location, active):
    location.active = active
    location.save(update_fields=["active"])
    audit.log(admin, "location.update", location, active=active)
    return location


@transaction.atomic
def merge_locations(admin, source, target):
    """Move the photos and proposals of `source` to `target`; `source` stays as a pointer."""
    if source.pk == target.pk:
        raise ValidationError(_("No se puede fusionar una ubicación consigo misma."))
    source = Location.objects.select_for_update().get(pk=source.pk)
    target = Location.objects.select_for_update().get(pk=target.pk)
    if source.merged_into_id or target.merged_into_id:
        raise ValidationError(_("Solo se pueden fusionar ubicaciones que no estén ya fusionadas."))
    Photo.objects.filter(location=source).update(location=target, catalog_changed_at=timezone.now())
    Proposal.objects.filter(location=source).update(location=target)
    if target.is_provisional and not source.is_provisional:
        target.is_provisional = False
        target.save(update_fields=["is_provisional"])
    source.merged_into = target
    source.active = False
    source.is_provisional = False
    source.save(update_fields=["merged_into", "active", "is_provisional"])
    audit.log(admin, "location.merge", target, source_id=source.pk, source_name=source.name)
    return target
