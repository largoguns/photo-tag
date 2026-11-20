"""Material 3 shell, gallery sections and filters, photo viewer, zoom policy and icons."""

import datetime
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse

from catalog.models import Face, Person, Photo, Scan

pytestmark = pytest.mark.django_db

BASE_DIR = Path(settings.BASE_DIR)


def make_photo(index, date=None, precision="", scan=None):
    scan = scan or Scan.objects.get_or_create(sha256="e" * 64, defaults={"file_name": "s.jpg"})[0]
    return Photo.objects.create(
        scan=scan,
        file_name=f"p{index}.jpg",
        index=index,
        width=800 if index % 2 else 600,
        height=600 if index % 2 else 800,
        date_value=date,
        date_precision=precision,
    )


# --- App shell ---------------------------------------------------------------------


def test_page_zoom_is_disabled():
    base = (BASE_DIR / "templates" / "base.html").read_text()
    assert "maximum-scale=1, user-scalable=no" in base
    css = (BASE_DIR / "static" / "css" / "app.css").read_text()
    assert "touch-action: pan-x pan-y" in css


def test_shell_has_top_bar_avatar_menu_nav_and_no_more_button(client, superadmin):
    client.force_login(superadmin)
    content = client.get(reverse("home")).content.decode()
    assert 'class="top-app-bar"' in content
    assert 'class="avatar-button"' in content
    assert reverse("profile") in content and reverse("user_list") in content
    assert content.count('class="nav-item"') == 4  # Inicio, Galería, Personas, Árbol
    assert "nav-more" not in content
    assert 'class="fab"' in content and reverse("upload") in content


def test_avatar_uses_linked_person_face(client, alice, photo):
    person = Person.objects.create(display_name="Alicia", user=alice)
    Face.objects.filter(pk=photo.faces.first().pk).update(person=person)
    client.force_login(alice)
    content = client.get(reverse("home")).content.decode()
    assert reverse("face_image", args=[photo.faces.first().pk]) in content


def test_no_shell_before_password_change(client, alice):
    alice.must_change_password = True
    alice.save()
    client.force_login(alice)
    content = client.get(reverse("password_change_forced")).content.decode()
    assert "top-app-bar" not in content and "nav-bar" not in content


def test_push_prompt_only_with_vapid_keys(client, alice, settings):
    client.force_login(alice)
    settings.VAPID_PUBLIC_KEY = settings.VAPID_PRIVATE_KEY = ""
    content = client.get(reverse("home")).content.decode()
    assert "data-push-key" not in content and 'id="push-banner"' not in content

    settings.VAPID_PUBLIC_KEY, settings.VAPID_PRIVATE_KEY = "pub", "priv"
    content = client.get(reverse("home")).content.decode()
    assert 'data-push-key="pub"' in content and 'id="push-banner"' in content


def test_every_icon_used_exists_in_the_sprite():
    sprite = (BASE_DIR / "static" / "img" / "icons.svg").read_text()
    available = set(re.findall(r'<symbol id="i-([\w-]+)"', sprite))
    used = set()
    for template in (BASE_DIR / "templates").rglob("*.html"):
        used |= set(re.findall(r'{% icon "([\w-]+)"', template.read_text()))
    assert used, "no icons found"
    assert used <= available, used - available


# --- Gallery -----------------------------------------------------------------------


def test_gallery_is_grouped_by_year_newest_first(client, alice, data_dirs):
    make_photo(1, datetime.date(1985, 7, 1), "exact")
    make_photo(2, datetime.date(1990, 1, 1), "year")
    make_photo(3, datetime.date(1980, 1, 1), "decade")
    make_photo(4, datetime.date(1980, 1, 1), "year")
    make_photo(5)
    client.force_login(alice)

    response = client.get(reverse("gallery"))

    sections = [label for key, label, count in response.context["sections"]]
    assert sections == ["1990", "1985", "1980", "Década de 1980", "Sin fecha"]
    content = response.content.decode()
    assert content.count('class="gallery-section-title"') == 5
    assert 'class="justified"' in content  # the first blocks come filled with the page


def test_filter_chips_and_sheets(client, alice, boda, data_dirs):
    make_photo(1)
    client.force_login(alice)
    content = client.get(reverse("gallery"), {"event": boda.pk}).content.decode()
    assert 'data-open-sheet="sheet-event"' in content
    assert 'id="sheet-person"' in content and "data-sheet-search" in content
    assert "Borrar filtros" in content
    # Chosen event shows as a selected chip with its name.
    assert re.search(r'class="chip selected"[^>]*data-open-sheet="sheet-event"', content)


def test_jitem_uses_dot_decimal_aspect_ratio(client, alice, data_dirs):
    make_photo(1)  # 800x600
    client.force_login(alice)
    assert "--r: 1.3333" in client.get(reverse("gallery")).content.decode()


# --- Photo page ---------------------------------------------------------------------


def test_photo_page_has_viewer_toolbar_and_combobox(client, alice, photo):
    client.force_login(alice)
    content = client.get(reverse("photo_detail", args=[photo.pk])).content.decode()
    assert 'id="photo-viewer"' in content
    assert reverse("photo_image", args=[photo.pk, "full"]) in content
    assert 'data-viewer="faces"' in content and 'data-viewer="zoom-in"' in content
    assert 'data-action="open-viewer"' in content
    assert "data-person-combobox" in content and 'role="combobox"' in content
    assert 'id="people-data"' in content
    assert 'list="person-names"' not in content


def test_people_json_includes_avatars(client, alice, photo):
    person = Person.objects.create(display_name="Carmen")
    face = photo.faces.first()
    Face.objects.filter(pk=face.pk).update(person=person)
    client.force_login(alice)
    response = client.get(reverse("photo_detail", args=[photo.pk]))
    entry = next(p for p in response.context["people_json"] if p["name"] == "Carmen")
    assert entry["avatar"] == reverse("face_image", args=[face.pk])


def test_hidden_attribute_always_wins():
    # Regression: .draw-form { display: flex } made the hidden "new face" form visible.
    css = (BASE_DIR / "static" / "css" / "app.css").read_text()
    assert "[hidden] { display: none !important; }" in css


def test_languages_are_named_in_their_own_language(client, alice):
    client.force_login(alice)
    content = client.get(reverse("home"), HTTP_ACCEPT_LANGUAGE="es").content.decode()
    assert ">Español</button>" in content and ">English</button>" in content


@pytest.mark.parametrize(
    ("name", "back"),
    [
        ("admin_event_types", "admin_home"),
        ("admin_thresholds", "admin_home"),
        ("admin_locations", "admin_home"),
        ("admin_review_queue", "admin_home"),
        ("admin_merge", "admin_home"),
        ("export_home", "admin_home"),
        ("scan", "admin_home"),
        ("user_list", "admin_home"),
        ("user_create", "user_list"),
    ],
)
def test_admin_screens_have_a_back_arrow_header(client, superadmin, name, back):
    client.force_login(superadmin)
    content = client.get(reverse(name)).content.decode()
    header = content.split('class="page-title"')[1].split("</div>")[0]
    assert f'href="{reverse(back)}"' in header and "back-button" in header
    assert "← " not in content


def test_profile_is_a_settings_list_with_collapsible_rows(client, alice):
    client.force_login(alice)
    content = client.get(reverse("profile")).content.decode()
    assert 'class="settings-list"' in content
    for row in ["setting-name", "setting-theme", "setting-language", "setting-password"]:
        assert re.search(rf'<details class="setting" id="{row}"\s*>', content)
    assert 'name="save_profile"' in content and 'name="change_password"' in content


def test_profile_opens_the_row_with_errors(client, alice):
    client.force_login(alice)
    response = client.post(
        reverse("profile"),
        {"change_password": "1", "new_password1": "a", "new_password2": "b"},
    )
    content = response.content.decode()
    assert '<details class="setting" id="setting-password" open>' in content
    assert re.search(r'<details class="setting" id="setting-name"\s*>', content)


def test_profile_has_a_side_index_for_wide_windows(client, alice):
    client.force_login(alice)
    content = client.get(reverse("profile")).content.decode()
    assert "data-settings" in content and 'class="settings-nav"' in content
    for target in ("#setting-name", "#setting-theme", "#setting-language", "#setting-password"):
        assert f'href="{target}"' in content
