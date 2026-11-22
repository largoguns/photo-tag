"""Map of where photos were taken (spec section 5.15)."""

import json

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from accounts.models import AuditLog
from catalog.models import Location, Photo, Proposal
from catalog.services import catalog_review, geo, voting
from catalog.services import locations as location_service
from catalog.services.names import name_key

pytestmark = pytest.mark.django_db


def place(name, lat=None, lng=None, locality=""):
    return Location.objects.create(
        name=name, name_key=name_key(name), latitude=lat, longitude=lng, locality=locality
    )


# --- Proposing and reviewing ---------------------------------------------------------------


def test_a_new_place_keeps_the_proposed_locality(photo, alice):
    proposal = voting.create_proposal(
        alice,
        photo,
        Proposal.Kind.LOCATION,
        location_name="Piso Los Tarantos",
        locality=" Sevilla ",
    )
    assert proposal.location.locality == "Sevilla" and proposal.location.is_provisional


def test_an_existing_place_only_gets_a_locality_it_did_not_have(photo, alice):
    empty = place("Casa Greco")
    known = place("Club de Campo", locality="Madrid")
    voting.resolve_location("casa greco", alice, locality="Sevilla")
    voting.resolve_location("Club de Campo", alice, locality="Toledo")
    empty.refresh_from_db()
    known.refresh_from_db()
    assert empty.locality == "Sevilla" and known.locality == "Madrid"


def test_the_administrator_reviews_the_locality(photo, alice, admin_user):
    proposal = voting.create_proposal(
        alice, photo, Proposal.Kind.LOCATION, location_name="Ronda", locality="Malaga"
    )
    catalog_review.accept(admin_user, proposal.location, "Ronda", locality="Ronda (Málaga)")
    proposal.location.refresh_from_db()
    assert proposal.location.locality == "Ronda (Málaga)" and not proposal.location.is_provisional


def test_the_photo_form_asks_for_the_locality(client, photo, alice):
    client.force_login(alice)
    content = client.get(reverse("photo_detail", args=[photo.pk])).content.decode()
    assert 'name="locality"' in content and "Localidad (opcional)" in content


# --- Placing --------------------------------------------------------------------------------


def test_the_location_page_keeps_the_exact_point_in_spanish(client, admin_user, settings):
    """Regression: Spanish renders 37.389092 as "37,389092", which the page's JavaScript
    read as 37, moving the pin to (37, -5) and saving that on the next «Guardar»."""
    location = place("Piso Los Tarantos", 37.389092, -5.984459, "Sevilla")
    client.force_login(admin_user)
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "es"
    content = client.get(reverse("admin_location_edit", args=[location.pk])).content.decode()
    assert 'data-lat="37.389092" data-lng="-5.984459"' in content
    assert 'name="latitude" value="37.389092"' in content
    assert 'name="longitude" value="-5.984459"' in content

    # Saving the town without moving the pin keeps the point.
    url = reverse("admin_location_edit", args=[location.pk])
    client.post(
        url,
        {
            "action": "place",
            "latitude": "37.389092",
            "longitude": "-5.984459",
            "locality": "Triana",
        },
    )
    location.refresh_from_db()
    assert (location.latitude, location.longitude, location.locality) == (
        37.389092,
        -5.984459,
        "Triana",
    )


def test_a_decimal_comma_is_never_truncated(admin_user):
    location = place("X")
    location_service.place_location(admin_user, location, "37,389092", "-5,984459")
    location.refresh_from_db()
    assert (location.latitude, location.longitude) == (37.389092, -5.984459)


def test_place_and_unplace(admin_user):
    location = place("Piso Los Tarantos")
    location_service.place_location(admin_user, location, "37.389092", "-5.984459", "Sevilla")
    location.refresh_from_db()
    assert location.is_placed and location.locality == "Sevilla"
    assert (location.latitude, location.longitude) == (37.389092, -5.984459)
    assert AuditLog.objects.filter(action="location.place").exists()

    location_service.place_location(admin_user, location, None, None)
    location.refresh_from_db()
    assert not location.is_placed and location.locality == "Sevilla"
    assert AuditLog.objects.filter(action="location.unplace").exists()


@pytest.mark.parametrize(("lat", "lng"), [("91", "0"), ("0", "-181"), ("norte", "0")])
def test_positions_are_validated(admin_user, lat, lng):
    with pytest.raises(ValidationError):
        location_service.place_location(admin_user, place("X"), lat, lng)


def test_merging_inherits_the_point_and_town(admin_user):
    source = place("Casa de la abuela", 37.0, -6.0, "Gerena")
    target = place("Casa Abuela")
    location_service.merge_locations(admin_user, source, target)
    target.refresh_from_db()
    assert target.is_placed and target.locality == "Gerena"


def test_location_page_places_it(client, admin_user, alice):
    location = place("Chiclana")
    url = reverse("admin_location_edit", args=[location.pk])
    client.force_login(alice)
    assert client.post(url, {"action": "place", "latitude": "1", "longitude": "1"}).status_code in (
        302,
        403,
    )
    location.refresh_from_db()
    assert not location.is_placed

    client.force_login(admin_user)
    content = client.get(url).content.decode()
    assert (
        "data-location-picker" in content and "Chiclana" in content and "data-geo-search" in content
    )
    client.post(
        url, {"action": "place", "latitude": "36.41", "longitude": "-6.15", "locality": "Chiclana"}
    )
    location.refresh_from_db()
    assert location.is_placed
    client.post(url, {"action": "unplace"})
    location.refresh_from_db()
    assert not location.is_placed


def test_locations_list_shows_what_is_not_on_the_map(client, admin_user):
    place("Placed", 1, 1, "Sevilla")
    place("Lost")
    client.force_login(admin_user)
    content = client.get(reverse("admin_locations")).content.decode()
    assert "Sin situar en el mapa (1)" in content and "Sevilla" in content
    only = client.get(reverse("admin_locations"), {"unplaced": "1"}).context["locations"]
    assert [loc.name for loc in only] == ["Lost"]
    assert "1 sin situar en el mapa" in client.get(reverse("admin_home")).content.decode()


# --- The map ------------------------------------------------------------------------------


def test_map_lists_placed_locations_with_visible_photos(photo):
    home = place("Casa", 37.1, -5.9, "Sevilla")
    place("Sin fotos", 40.0, -3.0)
    lost = place("Sin situar")
    Photo.objects.filter(pk=photo.pk).update(location=home)
    extra = Photo.objects.create(
        scan=photo.scan, file_name="b.jpg", index=1, width=10, height=10, location=lost
    )
    hidden = Photo.objects.create(
        scan=photo.scan, file_name="c.jpg", index=2, width=10, height=10, location=home
    )
    Photo.objects.filter(pk=hidden.pk).update(discarded=True)

    places = geo.map_locations()

    assert [p["name"] for p in places] == ["Casa"]
    (casa,) = places
    assert casa["count"] == 1 and casa["locality"] == "Sevilla"
    assert casa["photos"][0]["id"] == photo.pk and casa["photos"][0]["thumb"].startswith("/photos/")
    assert casa["gallery"] == f"{reverse('gallery')}?location={home.pk}"
    assert extra.pk not in [p["id"] for p in casa["photos"]]


def test_map_previews_at_most_six_photos(photo):
    home = place("Casa", 37.1, -5.9)
    for index in range(8):
        Photo.objects.create(
            scan=photo.scan, file_name=f"m{index}.jpg", index=index + 1, width=10, height=10,
            location=home,
        )  # fmt: skip
    (casa,) = geo.map_locations()
    assert casa["count"] == 8 and len(casa["photos"]) == geo.PREVIEW_PHOTOS


def test_map_page(client, photo, alice, admin_user):
    assert client.get(reverse("photo_map")).status_code == 302  # login required

    client.force_login(alice)
    content = client.get(reverse("photo_map")).content.decode()
    assert "Todavía no hay ubicaciones situadas en el mapa" in content
    assert "Situar ubicaciones" not in content  # only administrators place them

    home = place("Casa", 37.1, -5.9)
    Photo.objects.filter(pk=photo.pk).update(location=home)
    response = client.get(reverse("photo_map"))
    content = response.content.decode()
    assert "data-photo-map" in content and "leaflet.markercluster.js" in content
    data = json.loads(
        content.split('id="map-places" type="application/json">')[1].split("</script>")[0]
    )
    assert data[0]["name"] == "Casa"
    assert 'class="nav-item" href="/map/" aria-current="page"' in content.replace(
        reverse("photo_map"), "/map/"
    )

    place("Sin situar")
    client.force_login(admin_user)
    assert "1 ubicación sin situar en el mapa" in client.get(reverse("photo_map")).content.decode()
