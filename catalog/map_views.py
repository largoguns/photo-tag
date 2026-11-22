"""Map of where photos were taken (spec section 5.15)."""

from django.shortcuts import render

from catalog.services import geo


def photo_map(request):
    """Placed locations with their photo count and thumbnails; static/js/map.js draws them."""
    places = geo.map_locations()
    return render(
        request,
        "catalog/map.html",
        {
            "places": places,
            "unplaced": geo.unplaced_count() if request.user.is_catalog_admin else 0,
        },
    )
