"""Gallery thumbnails show what is left to do; a cataloged photo shows nothing."""

import pytest
from django.template.loader import render_to_string

from catalog.models import Photo

pytestmark = pytest.mark.django_db


def icon(photo):
    html = render_to_string("catalog/_justified_item.html", {"photo": photo})
    return html.split('class="jitem-status ')[1].split('"')[0] if "jitem-status" in html else None


def test_status_icons(photo):
    photo.status = Photo.Status.PENDING
    assert icon(photo) == "pending"
    photo.status = Photo.Status.IN_PROGRESS
    assert icon(photo) == "in-progress"
    photo.status = Photo.Status.CATALOGED
    assert icon(photo) is None  # the shared gallery needs no «done» mark
    photo.status = Photo.Status.PENDING
    photo.pending_my_vote = True
    assert icon(photo) == "vote"  # what I can do comes first
