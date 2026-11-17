import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import translation

from accounts.models import AuditLog
from catalog.models import Location, Photo, Proposal, Vote
from catalog.services import locations, voting
from exporter.services import xmp
from gamification.models import ScoreEvent
from scanner.services import deletion

pytestmark = pytest.mark.django_db

Kind = Proposal.Kind
Status = Proposal.Status


@pytest.fixture(autouse=True)
def spanish():
    with translation.override("es"):
        yield


def propose(user, photo, name):
    return voting.create_proposal(user, photo, Kind.LOCATION, location_name=name)


def another_photo(photo, index=1):
    return Photo.objects.create(
        scan=photo.scan, file_name=f"other_{index}.jpg", index=index, width=800, height=600
    )


def test_new_location_is_provisional_and_kept_as_typed(photo, alice):
    proposal = propose(alice, photo, "  Casa de   Los Tarantos ")

    location = Location.objects.get()
    assert location.name == "Casa de Los Tarantos"
    assert location.is_provisional
    assert proposal.label() == "Casa de Los Tarantos"


def test_existing_location_is_reused_ignoring_accents_and_case(photo, alice):
    existing = Location.objects.create(name="Cádiz", name_key="cadiz")
    proposal = propose(alice, photo, "CADIZ")
    assert proposal.location == existing
    assert Location.objects.count() == 1


def test_merged_location_resolves_to_its_target(photo, alice):
    target = Location.objects.create(name="Madrid", name_key="madrid")
    Location.objects.create(name="Mdrid", name_key="mdrid", merged_into=target)
    assert propose(alice, photo, "mdrid").location == target


def test_accepting_sets_the_photo_location_and_makes_it_permanent(photo, alice, bob):
    proposal = propose(alice, photo, "Playa de Bolonia")
    proposal = voting.cast_vote(bob, proposal, Vote.UP)

    assert proposal.status == Status.ACCEPTED
    photo.refresh_from_db()
    assert photo.location.name == "Playa de Bolonia"
    assert not photo.location.is_provisional
    assert photo.catalog_changed_at is not None
    with pytest.raises(ValidationError, match="ya está aceptada"):
        propose(alice, photo, "playa de bolonia")


def test_location_is_not_required_to_be_cataloged(photo, alice, bob, boda):
    voting.create_proposal(alice, photo, Kind.EVENT, event_type=boda)
    for face in photo.faces.all():
        p = voting.create_proposal(alice, photo, Kind.FACE_IDENTITY, face=face, person_name="X")
        voting.cast_vote(bob, p, Vote.UP)
    event = Proposal.objects.get(kind=Kind.EVENT)
    voting.cast_vote(bob, event, Vote.UP)
    photo.refresh_from_db()
    assert photo.status == Photo.Status.CATALOGED


def test_rejected_or_superseded_provisional_location_is_deleted(photo, alice, bob, carol):
    rejected = propose(alice, photo, "Sitio raro")
    voting.cast_vote(bob, rejected, Vote.DOWN)
    voting.cast_vote(carol, rejected, Vote.DOWN)
    assert not Location.objects.filter(name="Sitio raro").exists()
    rejected.refresh_from_db()
    assert rejected.label() == "Sitio raro"  # the name is kept in the proposal

    loser = propose(alice, photo, "Otro sitio")
    winner = propose(alice, photo, "Buen sitio")
    voting.cast_vote(bob, winner, Vote.UP)
    loser.refresh_from_db()
    assert loser.status == Status.SUPERSEDED
    assert list(Location.objects.values_list("name", flat=True)) == ["Buen sitio"]


def test_identical_open_proposal_adds_a_vote(photo, alice, bob):
    first = propose(alice, photo, "Ronda")
    second = propose(bob, photo, "ronda")
    assert second.pk == first.pk
    assert second.status == Status.ACCEPTED


def test_deleting_the_photo_cleans_the_provisional_location(photo, alice, admin_user):
    propose(alice, photo, "Solo aquí")
    deletion.delete_photo(admin_user, photo)
    assert not Location.objects.exists()


def test_photo_view_panel_and_suggestions(client, photo, alice):
    Location.objects.create(name="Casa de los abuelos", name_key="casa de los abuelos")
    Location.objects.create(name="Oculta", name_key="oculta", active=False)
    client.force_login(alice)

    content = client.get(reverse("photo_detail", args=[photo.pk])).content.decode()

    assert 'id="location-panel"' in content and 'data-combobox="locations"' in content
    assert 'id="locations-data"' in content
    assert "Casa de los abuelos" in content and "Oculta" not in content

    response = client.post(
        reverse("propose_location", args=[photo.pk]), {"location": ""}, HTTP_HX_REQUEST="true"
    )
    assert "Escribe dónde se hizo la foto" in response.content.decode()


def test_gallery_filters_by_location_with_counts(client, photo, alice):
    home = Location.objects.create(name="Casa", name_key="casa")
    old = Location.objects.create(name="Kasa", name_key="kasa", merged_into=home)
    photo.location = home
    photo.save()
    another_photo(photo)
    client.force_login(alice)

    content = client.get(reverse("gallery"), {"location": home.pk}).content.decode()
    assert "1 foto" in content
    assert 'data-open-sheet="sheet-location"' in content
    assert '<span class="option-count">1</span>' in content

    # A merged location in the URL shows its target.
    merged = client.get(reverse("gallery"), {"location": old.pk}).content.decode()
    assert "1 foto" in merged


def test_admin_renames_merges_and_deactivates(client, photo, admin_user, alice):
    a = Location.objects.create(name="Casa abuela", name_key="casa abuela")
    b = Location.objects.create(name="Casa de la abuela", name_key="casa de la abuela")
    photo.location = a
    photo.save()
    open_proposal = propose(alice, another_photo(photo), "Casa abuela")

    with pytest.raises(ValidationError, match="Ya existe"):
        locations.rename_location(admin_user, a, "casa de la ABUELA")
    locations.rename_location(admin_user, b, "Casa de la abuela Carmen")
    locations.merge_locations(admin_user, a, b)

    photo.refresh_from_db()
    open_proposal.refresh_from_db()
    a.refresh_from_db()
    assert photo.location == b and photo.catalog_changed_at is not None
    assert open_proposal.location == b
    assert a.merged_into == b and not a.active
    locations.set_active(admin_user, b, False)
    assert not voting.locations().exists()
    actions = set(AuditLog.objects.values_list("action", flat=True))
    assert {"location.rename", "location.merge", "location.update"} <= actions


def test_admin_screens(client, admin_user, alice):
    location = Location.objects.create(name="Cádiz", name_key="cadiz")
    client.force_login(alice)
    assert client.get(reverse("admin_locations")).status_code == 403
    client.force_login(admin_user)

    assert "Cádiz" in client.get(reverse("admin_locations")).content.decode()
    url = reverse("admin_location_edit", args=[location.pk])
    assert client.get(url).status_code == 200
    client.post(url, {"action": "rename", "name": "Cádiz capital"})
    client.post(url, {"action": "deactivate"})
    location.refresh_from_db()
    assert location.name == "Cádiz capital" and not location.active


def test_export_writes_the_location(photo, boda):
    photo.location = Location.objects.create(name="Casa / campo", name_key="casa / campo")
    photo.event_type = boda
    photo.save()

    assert "Ubicaciones/Casa - campo" in xmp.tags(photo)
    args = xmp.exiftool_args(photo, "out.xmp")
    assert "-XMP-iptcCore:Location=Casa / campo" in args
    assert "-XMP-dc:Subject=Ubicaciones/Casa - campo" in args


def test_accepted_location_scores_ten_points(photo, alice, bob):
    voting.cast_vote(bob, propose(alice, photo, "Huelva"), Vote.UP)
    assert ScoreEvent.objects.get(user=alice, reason="proposal.location").points == 10
