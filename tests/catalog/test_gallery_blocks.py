"""Gallery in blocks (skeleton + per-block loading) and the several-people filter."""

import datetime
import re

import pytest
from django.http import QueryDict
from django.urls import reverse

from catalog.models import Face, Person, Photo, Scan
from catalog.services import gallery
from catalog.services.persons import merge_persons

pytestmark = pytest.mark.django_db


@pytest.fixture
def scan(db):
    return Scan.objects.create(sha256="c" * 64, file_name="s.jpg")


def make(scan, index, date=None, precision="", width=800, height=600):
    return Photo.objects.create(
        scan=scan,
        file_name=f"p{index}.jpg",
        index=index,
        width=width,
        height=height,
        date_value=date,
        date_precision=precision,
    )


def everything(user):
    return gallery.gallery_queryset(QueryDict(""), user)


def test_blocks_cut_sections_and_only_the_first_has_a_header(scan, alice, monkeypatch):
    monkeypatch.setattr(gallery, "BLOCK_SIZE", 2)
    for i in range(5):
        make(scan, i, datetime.date(1985, 1, 1), "year")
    make(scan, 9, datetime.date(1990, 1, 1), "year", width=600, height=600)

    blocks = gallery.blocks(everything(alice))

    assert [(b.key, b.count, b.header) for b in blocks] == [
        ("y1990", 1, True),
        ("y1985", 2, True),
        ("y1985", 2, False),
        ("y1985", 1, False),
    ]
    assert blocks[0].ratio == pytest.approx(1.0)
    assert blocks[1].ratio == pytest.approx(800 / 600 * 2)
    assert gallery.sections(blocks) == [("y1990", "1990", 1), ("y1985", "1985", 5)]


def test_block_photos_keep_the_gallery_order(scan, alice):
    photos = [make(scan, i, datetime.date(1985, 1, 1 + i), "exact") for i in range(3)]
    (first, *_rest) = gallery.blocks(everything(alice), size=10)
    assert [p.pk for p in gallery.block_photos(first, alice)] == [p.pk for p in photos[::-1]]


def test_page_is_a_skeleton_with_the_first_blocks_filled(client, scan, alice, monkeypatch):
    monkeypatch.setattr(gallery, "BLOCK_SIZE", 2)
    for i in range(9):
        make(scan, i, datetime.date(1985, 1, 1), "year")
    client.force_login(alice)

    content = client.get(reverse("gallery")).content.decode()

    assert content.count('class="gallery-block"') == 5
    assert content.count("data-loaded") == 2  # blocks 0 and 1 come with the page
    assert content.count('class="jitem"') == 4
    assert "9 fotos" in content


def test_a_block_is_loaded_on_its_own(client, scan, alice, monkeypatch):
    monkeypatch.setattr(gallery, "BLOCK_SIZE", 2)
    for i in range(5):
        make(scan, i, datetime.date(1985, 1, 1), "year")
    client.force_login(alice)

    fragment = client.get(reverse("gallery"), {"block": 2}, HTTP_HX_REQUEST="true")
    assert fragment.content.decode().count('class="jitem"') == 1
    assert "<html" not in fragment.content.decode()
    missing = client.get(reverse("gallery"), {"block": 9}, HTTP_HX_REQUEST="true")
    assert missing.status_code == 404


def test_anchor_brings_the_block_where_the_user_was(client, scan, alice, monkeypatch):
    monkeypatch.setattr(gallery, "BLOCK_SIZE", 2)
    for i in range(12):
        make(scan, i, datetime.date(1985, 1, 1), "year")
    client.force_login(alice)
    content = client.get(reverse("gallery"), {"anchor": 4}).content.decode()
    loaded = re.findall(r'data-block="(\d+)"[^>]*data-loaded', content)
    assert loaded == ["3", "4", "5"]
    assert 'data-anchor="4"' in content


def test_year_rail_links_to_each_section(client, scan, alice):
    make(scan, 1, datetime.date(1985, 1, 1), "year")
    make(scan, 2, datetime.date(1990, 1, 1), "year")
    client.force_login(alice)
    content = client.get(reverse("gallery")).content.decode()
    assert 'href="#section-y1990"' in content and 'id="section-y1985"' in content


def test_empty_gallery(client, alice):
    client.force_login(alice)
    content = client.get(reverse("gallery")).content.decode()
    assert "No hay fotos con estos filtros." in content and "year-rail" not in content


# --- Several people: «salen todas juntas» ----------------------------------------------


@pytest.fixture
def family(scan):
    ana, luis = (
        Person.objects.create(display_name="Ana"),
        Person.objects.create(display_name="Luis"),
    )
    both = make(scan, 1)
    only_ana = make(scan, 2)
    for photo, people in ((both, [ana, luis]), (only_ana, [ana])):
        for person in people:
            Face.objects.create(photo=photo, x=1, y=1, w=10, h=10, person=person)
    return ana, luis, both, only_ana


def chosen(user, *people):
    query = QueryDict("", mutable=True)
    query.setlist("person", [str(p.pk) for p in people])
    return list(gallery.gallery_queryset(query, user))


def test_two_people_means_photos_with_both(alice, family):
    ana, luis, both, only_ana = family
    assert chosen(alice, ana, luis) == [both]
    assert set(chosen(alice, ana)) == {both, only_ana}


def test_merged_people_are_resolved(alice, admin_user, family):
    ana, luis, both, only_ana = family
    duplicate = Person.objects.create(display_name="Ana bis")
    merge_persons(admin_user, duplicate, ana)
    assert set(chosen(alice, duplicate)) == {both, only_ana}


def test_people_chip_and_sheet(client, alice, family):
    ana, luis, both, only_ana = family
    client.force_login(alice)
    content = client.get(reverse("gallery") + f"?person={ana.pk}&person={luis.pk}").content.decode()
    assert "Ana +1" in content
    assert content.count('name="person"') == 2 and "checked" in content
    assert f"?person={luis.pk}" in content  # removing Ana keeps Luis
    assert "1 foto" in content
