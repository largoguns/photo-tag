"""Confirmation dialog, light/dark theme and Spanish/English interface."""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse
from django.utils import translation

from catalog.models import EventType, Photo, Proposal, Scan
from catalog.services import voting
from exporter.services import xmp

pytestmark = pytest.mark.django_db

TEMPLATES = Path(settings.BASE_DIR) / "templates"
PO_FILE = Path(settings.BASE_DIR) / "locale" / "en" / "LC_MESSAGES" / "django.po"


# --- Confirmation dialog --------------------------------------------------------


def test_no_template_uses_the_browser_confirm():
    for template in TEMPLATES.rglob("*.html"):
        assert "confirm(" not in template.read_text(), template


def test_destructive_actions_ask_for_confirmation():
    expected = {
        "accounts/user_list.html": 2,  # reset password, deactivate
        "catalog/admin/review_queue.html": 3,  # discard crop, delete photo, delete scan
        "catalog/admin/merge.html": 1,
        "catalog/_proposal.html": 2,  # admin accept / reject
        # not a face, delete photo, reprocess scan, delete scan
        "catalog/_photo_workspace.html": 4,
    }
    for name, count in expected.items():
        text = (TEMPLATES / name).read_text()
        assert len(re.findall(r"\b(?:hx-confirm|data-confirm)=", text)) == count, name


def test_dialog_is_on_every_page(client, alice):
    client.force_login(alice)
    content = client.get(reverse("home")).content.decode()
    assert '<dialog id="confirm-dialog"' in content
    assert "data-confirm-ok" in content


# --- Theme -----------------------------------------------------------------------


def test_theme_follows_system_by_default(client, alice):
    client.force_login(alice)
    content = client.get(reverse("home")).content.decode()
    assert "data-theme=" not in content
    assert 'name="color-scheme" content="light dark"' in content


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_forced_theme_is_rendered(client, alice, theme):
    alice.theme = theme
    alice.save()
    client.force_login(alice)
    content = client.get(reverse("home")).content.decode()
    assert f'data-theme="{theme}"' in content


def test_css_defines_dark_tokens_for_system_and_forced_theme():
    css = (Path(settings.BASE_DIR) / "static" / "css" / "app.css").read_text()
    assert "@media (prefers-color-scheme: dark)" in css
    assert ':root:not([data-theme="light"])' in css
    assert ':root[data-theme="dark"]' in css


# --- Language ----------------------------------------------------------------------


def test_spanish_is_the_default(client, db):
    content = client.get(reverse("login")).content.decode()
    assert '<html lang="es"' in content
    assert "Iniciar sesión" in content


def test_browser_language_is_detected(client, db):
    content = client.get(reverse("login"), HTTP_ACCEPT_LANGUAGE="en-GB,en;q=0.9").content.decode()
    assert '<html lang="en"' in content
    assert "Sign in" in content


def test_language_switch_works_without_session_and_wins_over_browser(client, db):
    response = client.post(reverse("set_language"), {"language": "es", "next": reverse("login")})
    assert response.status_code == 302
    assert response.cookies[settings.LANGUAGE_COOKIE_NAME].value == "es"

    content = client.get(reverse("login"), HTTP_ACCEPT_LANGUAGE="en").content.decode()
    assert "Iniciar sesión" in content


def test_language_switch_is_allowed_before_password_change(client, alice):
    alice.must_change_password = True
    alice.save()
    client.force_login(alice)
    response = client.post(reverse("set_language"), {"language": "en"})
    assert response.status_code == 302
    assert response.url != reverse("password_change_forced")


def test_pages_render_in_english(client, photo, superadmin, boda):
    client.force_login(superadmin)
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
    voting.create_proposal(superadmin, photo, Proposal.Kind.EVENT, event_type=boda)

    content = client.get(reverse("photo_detail", args=[photo.pk])).content.decode()
    assert "Propose event" in content
    assert "Wedding" in content  # EventType.name_en
    assert "2 faces" in content
    for name in ["home", "gallery", "persons", "profile", "admin_home"]:
        assert client.get(reverse(name)).status_code == 200


def test_validation_errors_are_translated(client, photo, alice):
    client.force_login(alice)
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
    response = client.post(
        reverse("propose_date", args=[photo.pk]),
        {"precision": "year", "year": "two thousand"},
        HTTP_HX_REQUEST="true",
    )
    assert "Invalid year." in response.content.decode()


def test_event_type_english_name_falls_back_to_spanish():
    custom = EventType.objects.create(name="Romería")
    with translation.override("en"):
        assert str(EventType.objects.get(name="Boda")) == "Wedding"
        assert str(custom) == "Romería"
    with translation.override("es"):
        assert str(EventType.objects.get(name="Boda")) == "Boda"


def test_xmp_export_stays_in_spanish(photo, boda):
    photo.event_type = boda
    with translation.override("en"):
        assert xmp.tags(photo)[0] == "Eventos/Boda"


def test_review_reasons_are_translated():
    scan = Scan.objects.create(
        sha256="b" * 64,
        file_name="s.jpg",
        review_reasons=[{"code": "photo_count", "count": 7}, {"code": "overlap"}, "legacy"],
    )
    with translation.override("en"):
        assert scan.review_reason_labels() == [
            "7 photos were detected",
            "Overlapping crops",
            "legacy",
        ]
    with translation.override("es"):
        assert scan.review_reason_labels()[0] == "Se han detectado 7 fotos"


def test_dates_follow_the_language(photo):
    import datetime

    photo.date_precision = Photo.DatePrecision.EXACT
    photo.date_value = datetime.date(1985, 7, 14)
    with translation.override("es"):
        assert photo.date_label() == "14/07/1985"
    with translation.override("en"):
        assert photo.date_label() == "07/14/1985"


def test_translation_catalog_is_complete():
    text = PO_FILE.read_text(encoding="utf-8")
    entries = text.split("\n\n")[1:]
    assert entries
    assert "#, fuzzy" not in text
    for entry in entries:
        # An empty msgstr not followed by a continuation line is untranslated.
        assert not re.search(r'^msgstr(\[\d\])? ""(?!\n")', entry, re.M), entry
    assert PO_FILE.with_suffix(".mo").exists()
