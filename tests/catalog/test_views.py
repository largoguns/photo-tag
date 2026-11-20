import pytest
from django.urls import reverse

from catalog.models import Face, Person, Photo, Proposal, Scan
from catalog.services import voting

pytestmark = pytest.mark.django_db


def all_page_urls(photo):
    face = photo.faces.first()
    return [
        reverse("home"),
        reverse("gallery"),
        reverse("photo_detail", args=[photo.pk]),
        reverse("persons"),
        reverse("profile"),
        reverse("photo_image", args=[photo.pk, "400"]),
        reverse("photo_image", args=[photo.pk, "1600"]),
        reverse("photo_image", args=[photo.pk, "full"]),
        reverse("face_image", args=[face.pk]),
        reverse("admin_home"),
        reverse("user_list"),
        reverse("export_home"),
    ]


def test_anonymous_users_cannot_reach_any_page_or_image(client, photo):
    """Acceptance criterion 4."""
    for url in all_page_urls(photo):
        response = client.get(url)
        assert response.status_code == 302, url
        assert response.url.startswith(reverse("login")), url


def test_healthcheck_is_public_and_minimal(client, db):
    response = client.get(reverse("healthz"))
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_login_page_is_public(client, db):
    assert client.get(reverse("login")).status_code == 200


def test_pages_render_for_superadmin(client, photo, superadmin):
    client.force_login(superadmin)
    for url in all_page_urls(photo):
        assert client.get(url).status_code == 200, url


def test_admin_pages_forbidden_for_collaborators(client, alice, photo):
    client.force_login(alice)
    for name in [
        "admin_home",
        "admin_event_types",
        "admin_thresholds",
        "admin_review_queue",
        "admin_merge",
        "export_home",
    ]:
        assert client.get(reverse(name)).status_code == 403, name


def test_images_are_private_and_not_cached_publicly(client, alice, photo):
    client.force_login(alice)
    response = client.get(reverse("photo_image", args=[photo.pk, "400"]))
    assert response["Content-Type"] == "image/jpeg"
    assert response["Cache-Control"].startswith("private")


def test_unknown_image_size_is_404(client, alice, photo):
    client.force_login(alice)
    assert client.get(reverse("photo_image", args=[photo.pk, "9999"])).status_code == 404


def test_two_users_vote_same_name_through_the_web(client, photo, alice, bob):
    """Acceptance criterion 2, end to end through the views."""
    face = photo.faces.first()
    client.force_login(alice)
    client.post(reverse("propose_identity", args=[face.pk]), {"name": "Abuelo Paco"})
    proposal = Proposal.objects.get()

    client.force_login(bob)
    client.post(reverse("vote", args=[proposal.pk]), {"value": "up"})

    face.refresh_from_db()
    assert face.person.display_name == "Abuelo Paco"


def test_htmx_actions_return_the_workspace_fragment(client, photo, alice):
    client.force_login(alice)
    face = photo.faces.first()

    response = client.post(
        reverse("propose_identity", args=[face.pk]),
        {"name": "Abuelo Paco", "selected_face": face.pk},
        HTTP_HX_REQUEST="true",
    )

    content = response.content.decode()
    assert response.status_code == 200
    assert 'id="photo-workspace"' in content
    assert "<html" not in content
    assert "Abuelo Paco" in content


def test_validation_errors_are_shown(client, photo, alice):
    client.force_login(alice)
    response = client.post(
        reverse("propose_date", args=[photo.pk]),
        {"precision": "year", "year": "dos mil"},
        HTTP_HX_REQUEST="true",
    )
    assert "Año no válido" in response.content.decode()


def test_draw_face_and_date_and_event_from_views(client, photo, alice, boda):
    client.force_login(alice)
    client.post(reverse("propose_add_face", args=[photo.pk]), {"x": 10, "y": 10, "w": 50, "h": 50})
    client.post(
        reverse("propose_event", args=[photo.pk]), {"event_type": boda.pk, "description": "x"}
    )
    client.post(
        reverse("propose_date", args=[photo.pk]), {"precision": "exact", "date": "1990-05-02"}
    )
    client.post(reverse("propose_location", args=[photo.pk]), {"location": "Casa de la playa"})
    client.post(reverse("propose_remove_face", args=[photo.faces.first().pk]))

    kinds = set(Proposal.objects.values_list("kind", flat=True))
    assert kinds == {"add_face", "event", "date", "location", "remove_face"}


def test_photo_page_shows_face_boxes_with_dot_decimals(client, photo, alice):
    client.force_login(alice)
    content = client.get(reverse("photo_detail", args=[photo.pk])).content.decode()
    assert "left: 12.500%" in content
    assert content.count('class="face-box') == 2


def test_gallery_filters_and_infinite_scroll(client, photo, alice, bob, boda):
    client.force_login(alice)
    voting.create_proposal(bob, photo, Proposal.Kind.EVENT, event_type=boda)

    assert photo.pk in ids(client.get(reverse("gallery"), {"mine": "1"}))
    assert ids(client.get(reverse("gallery"), {"status": "cataloged"})) == []
    fragment = client.get(reverse("gallery"), {"block": 0}, HTTP_HX_REQUEST="true")
    assert "<html" not in fragment.content.decode()


def ids(response):
    return [pk for block in response.context["blocks"] for pk in block.ids]


def test_next_uncataloged_redirects(client, photo, alice):
    client.force_login(alice)
    response = client.get(reverse("next_uncataloged"))
    assert response.url == reverse("photo_detail", args=[photo.pk])


def test_discarded_photos_are_hidden(client, photo, alice, admin_user):
    client.force_login(admin_user)
    client.post(reverse("admin_photo_toggle_discard", args=[photo.pk]))
    photo.refresh_from_db()
    assert photo.discarded

    client.force_login(alice)
    assert ids(client.get(reverse("gallery"))) == []


def test_review_queue(client, admin_user, photo):
    Scan.objects.filter(pk=photo.scan_id).update(
        needs_review=True, review_reasons=["Recortes solapados"]
    )
    client.force_login(admin_user)

    assert "Recortes solapados" in client.get(reverse("admin_review_queue")).content.decode()
    client.post(reverse("admin_review_ok", args=[photo.scan_id]))
    assert Scan.objects.get(pk=photo.scan_id).reviewed


def test_admin_updates_thresholds_and_event_types(client, admin_user):
    client.force_login(admin_user)
    client.post(reverse("admin_thresholds"), {"vote_min_approvals": 3, "vote_approval_ratio": 0.75})
    client.post(reverse("admin_event_types"), {"name": "Graduación", "order": 5, "active": "on"})

    from catalog.models import CatalogSettings, EventType

    assert CatalogSettings.load().vote_min_approvals == 3
    assert EventType.objects.filter(name="Graduación").exists()


def test_admin_merges_persons_from_screen(client, admin_user, photo):
    a = Person.objects.create(display_name="Pepe")
    b = Person.objects.create(display_name="José")
    Face.objects.filter(pk=photo.faces.first().pk).update(person=a)
    client.force_login(admin_user)

    client.post(reverse("admin_merge"), {"source": a.pk, "target": b.pk})

    assert Face.objects.filter(person=b).count() == 1


def test_persons_list_and_detail(client, alice, photo):
    person = Person.objects.create(display_name="Pepe")
    Face.objects.filter(pk=photo.faces.first().pk).update(person=person)
    client.force_login(alice)

    content = client.get(reverse("persons")).content.decode()
    assert "Pepe" in content and "1 foto" in content
    assert client.get(reverse("person_detail", args=[person.pk])).context["page"][0] == photo


def test_admin_decide_from_view(client, admin_user, alice, photo):
    proposal = voting.create_proposal(
        alice, photo, Proposal.Kind.FACE_IDENTITY, face=photo.faces.first(), person_name="X"
    )
    client.force_login(alice)
    assert (
        client.post(reverse("decide", args=[proposal.pk]), {"decision": "accept"}).status_code
        == 403
    )

    client.force_login(admin_user)
    client.post(reverse("decide", args=[proposal.pk]), {"decision": "accept"})
    proposal.refresh_from_db()
    assert proposal.status == Proposal.Status.ACCEPTED


def test_home_shows_progress(client, alice, photo):
    Photo.objects.filter(pk=photo.pk).update(status=Photo.Status.CATALOGED)
    client.force_login(alice)
    response = client.get(reverse("home"))
    assert response.context["percent"] == 100


def test_persons_search_ignores_accents(client, alice, photo):
    Person.objects.create(display_name="María López")
    Person.objects.create(display_name="Pedro")
    client.force_login(alice)
    names = [p.display_name for p in client.get(reverse("persons"), {"q": "ari"}).context["page"]]
    assert names == ["María López"]
