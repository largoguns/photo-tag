"""Photos on the map (spec section 5.15).

Scanned photos have no coordinates: the map shows the *locations* that an administrator
placed (Location.latitude/longitude), each with how many visible photos it has and a few
thumbnails. Thumbnails are the usual login-protected images.
"""

from django.db.models import Count, Q
from django.urls import reverse

from catalog.models import Location, Photo

PREVIEW_PHOTOS = 6


def _thumb(photo):
    return f"{reverse('photo_image', args=[photo.pk, 400])}?v={photo.image_version}"


def map_locations():
    """[{id, name, locality, lat, lng, count, photos: [{id, thumb, url}]}] of the placed
    locations with at least one visible photo, most photographed first."""
    locations = list(
        Location.objects.filter(
            merged_into__isnull=True, latitude__isnull=False, longitude__isnull=False
        )
        .annotate(count=Count("photos", filter=Q(photos__discarded=False)))
        .filter(count__gt=0)
        .order_by("-count", "name")
    )
    previews = {}
    photos = (
        Photo.objects.visible()
        .filter(location__in=locations)
        .order_by("location_id", "-date_value", "-pk")
        .only("pk", "location_id", "image_version")
    )
    for photo in photos:
        mine = previews.setdefault(photo.location_id, [])
        if len(mine) < PREVIEW_PHOTOS:
            mine.append(
                {
                    "id": photo.pk,
                    "thumb": _thumb(photo),
                    "url": reverse("photo_detail", args=[photo.pk]),
                }
            )
    gallery = reverse("gallery")
    return [
        {
            "id": location.pk,
            "name": location.name,
            "locality": location.locality,
            "lat": location.latitude,
            "lng": location.longitude,
            "count": location.count,
            "photos": previews.get(location.pk, []),
            "gallery": f"{gallery}?location={location.pk}",
        }
        for location in locations
    ]


def unplaced_count():
    """Accepted locations still missing from the map (administration hint)."""
    return Location.objects.filter(
        merged_into__isnull=True, is_provisional=False, latitude__isnull=True
    ).count()
