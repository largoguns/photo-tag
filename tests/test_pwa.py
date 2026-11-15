"""Installable Progressive Web App: manifest, service worker, offline page and banner."""

import json
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.staticfiles import finders
from django.urls import reverse
from PIL import Image

pytestmark = pytest.mark.django_db

BASE_DIR = Path(settings.BASE_DIR)


def test_manifest_is_public_and_complete(client):
    response = client.get(reverse("pwa_manifest"))

    assert response.status_code == 200
    assert response["Content-Type"] == "application/manifest+json"
    data = json.loads(response.content)
    for key in ("name", "short_name", "start_url", "scope", "display", "icons", "theme_color"):
        assert data[key], key
    assert data["display"] == "standalone"
    assert data["scope"] == "/"
    purposes = {icon["purpose"] for icon in data["icons"]}
    assert purposes == {"any", "maskable"}


def test_manifest_icons_exist_with_declared_size(client):
    data = json.loads(client.get(reverse("pwa_manifest")).content)
    for icon in data["icons"]:
        relative = icon["src"].removeprefix(settings.STATIC_URL)
        path = finders.find(relative)
        assert path, icon["src"]
        with Image.open(path) as image:
            assert f"{image.width}x{image.height}" == icon["sizes"]
    sizes = {icon["sizes"] for icon in data["icons"]}
    assert {"192x192", "512x512"} <= sizes


def test_manifest_is_translated(client):
    es = json.loads(client.get(reverse("pwa_manifest")).content)
    en = json.loads(client.get(reverse("pwa_manifest"), HTTP_ACCEPT_LANGUAGE="en").content)
    assert es["lang"] == "es" and en["lang"] == "en"
    assert es["description"] != en["description"]


def test_service_worker_is_served_from_root(client):
    response = client.get("/sw.js")

    assert response.status_code == 200
    assert response["Content-Type"].startswith("application/javascript")
    assert "no-cache" in response["Cache-Control"]
    body = response.content.decode()
    assert "/static/css/app.css" in body
    assert '"/offline/"' in body


def test_service_worker_never_caches_private_content(client):
    body = client.get("/sw.js").content.decode()
    # Only static files and the offline page are put in the cache.
    assert "/photos/" not in body
    assert "/faces/" not in body
    assert body.count("cache.put(") == 1
    assert "url.pathname.startsWith(STATIC_PREFIX)" in body


def test_service_worker_version_is_stable_between_requests(client):
    assert client.get("/sw.js").content == client.get("/sw.js").content


def test_offline_page_is_public_and_standalone(client):
    response = client.get(reverse("pwa_offline"))
    content = response.content.decode()
    assert response.status_code == 200
    assert "Sin conexión" in content
    assert "csrf" not in content.lower()  # cached on the device: no tokens


@pytest.mark.parametrize("logged_in", [False, True])
def test_pages_link_manifest_icons_and_install_banner(client, alice, logged_in):
    if logged_in:
        client.force_login(alice)
    url = reverse("home") if logged_in else reverse("login")
    content = client.get(url).content.decode()

    assert (
        '<link rel="manifest" href="/manifest.webmanifest" crossorigin="use-credentials">'
        in content
    )
    assert 'rel="apple-touch-icon"' in content
    assert 'name="theme-color"' in content
    assert 'id="install-banner"' in content
    assert 'data-install-hint="ios"' in content
    assert 'data-install-action="install"' in content


def test_install_banner_is_translated(client):
    content = client.get(reverse("login"), HTTP_ACCEPT_LANGUAGE="en").content.decode()
    assert "Install PhotoTag" in content
    assert "Add to Home Screen" in content


def test_pwa_urls_are_reachable_before_password_change(client, alice):
    alice.must_change_password = True
    alice.save()
    client.force_login(alice)
    for name in ("pwa_manifest", "pwa_service_worker", "pwa_offline"):
        assert client.get(reverse(name)).status_code == 200, name


@pytest.mark.skipif(
    not (BASE_DIR / "docker-compose.yml").exists(), reason="compose file is not in the image"
)
def test_compose_builds_from_git_and_runs_as_nas_user():
    compose = (BASE_DIR / "docker-compose.yml").read_text()
    assert "pull_policy: build" in compose
    assert "pull_policy: never" in compose
    assert 'user: "${PUID:-1000}:${PGID:-100}"' in compose


def test_unhashed_static_files_are_not_served_stale(client):
    # Regression: in development (no hashed names) the cache-first worker kept old CSS.
    body = client.get("/sw.js").content.decode()
    assert "hashed" in body and "fetch(request).then(store).catch" in body


def test_cache_version_changes_with_file_content(monkeypatch, tmp_path):
    from phototag import pwa

    css = tmp_path / "app.css"
    css.write_text("a{}")
    monkeypatch.setattr(pwa, "SHELL_STATIC", ["css/app.css"])
    monkeypatch.setattr(pwa.finders, "find", lambda path: str(css))
    before = pwa.shell_version(["/static/css/app.css"])
    css.write_text("a{color:red}")
    assert pwa.shell_version(["/static/css/app.css"]) != before
