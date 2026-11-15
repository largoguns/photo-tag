"""My activity: filters with the same chips and sheets as the gallery."""

import pytest
from django.urls import reverse

from catalog.models import Proposal
from catalog.services import voting

pytestmark = pytest.mark.django_db


@pytest.fixture
def proposals(alice, photo):
    event = voting.create_proposal(alice, photo, Proposal.Kind.EVENT, event_name="Boda")
    date = voting.create_proposal(
        alice, photo, Proposal.Kind.DATE, value=voting.normalize_date("year", 1985)
    )
    return event, date


def test_filter_bar_like_the_gallery(client, alice, proposals):
    client.force_login(alice)
    content = client.get(reverse("my_activity")).content.decode()
    assert 'class="filter-bar"' in content
    assert 'data-open-sheet="sheet-status"' in content and 'id="sheet-kind"' in content
    assert "Borrar filtros" not in content and "2 propuestas" in content


def test_filter_by_kind(client, alice, proposals):
    client.force_login(alice)
    content = client.get(reverse("my_activity"), {"kind": "event"}).content.decode()
    assert "1 propuesta" in content and "Borrar filtros" in content
    assert "Boda" in content and "1985" not in content


def test_filter_by_status_without_results(client, alice, proposals):
    client.force_login(alice)
    content = client.get(reverse("my_activity"), {"status": "rejected"}).content.decode()
    assert "Ninguna propuesta con estos filtros." in content


def test_unknown_filter_values_are_ignored(client, alice, proposals):
    client.force_login(alice)
    content = client.get(reverse("my_activity"), {"kind": "x", "status": "y"}).content.decode()
    assert "2 propuestas" in content
