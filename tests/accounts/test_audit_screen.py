"""Audit screen: everything recorded, filterable by person, event and period."""

import re
from datetime import timedelta
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import AuditLog
from accounts.services import audit, audit_catalog
from catalog.models import Proposal, Vote
from catalog.services import voting

pytestmark = pytest.mark.django_db

PASSWORD = "Contrasena-segura-1"


def listed(response):
    """Only the timeline (the event filter sheet also lists every label)."""
    content = response.content.decode()
    return content.split('<ul class="list audit-list">')[1].split("</ul>")[0]


def sign_in(client, user, password=PASSWORD):
    return client.post(reverse("login"), {"username": user.username, "password": password})


def test_only_the_superadmin_sees_it(client, superadmin, admin_user, alice):
    for user in (admin_user, alice):
        client.force_login(user)
        assert client.get(reverse("audit")).status_code == 403
    client.force_login(superadmin)
    assert client.get(reverse("audit")).status_code == 200


def test_every_recorded_action_has_a_readable_label():
    # Any audit.log(..., "x.y") added in the code must be added to the catalogue too.
    pattern = re.compile(r'audit\.log\([^,]+,\s*"([a-z_]+\.[a-z_]+)"')
    pattern_alt = re.compile(r'"([a-z_]+\.[a-z_]+)" if [^"]+ else "([a-z_]+\.[a-z_]+)"')
    found = set()
    for path in Path(settings.BASE_DIR).glob("*/**/*.py"):
        if ".venv" in path.parts or "tests" in path.parts:
            continue
        text = path.read_text()
        found |= set(pattern.findall(text))
        for pair in pattern_alt.findall(text):
            found |= set(pair)
    assert found, "no audit actions found"
    assert found <= set(audit_catalog.EVENTS), found - set(audit_catalog.EVENTS)


def test_sign_ins_and_failures_appear_in_the_timeline(client, superadmin, alice):
    sign_in(client, alice, "wrong-password")
    sign_in(client, alice)
    client.force_login(superadmin)
    content = client.get(reverse("audit"), {"user": alice.pk}).content.decode()
    assert "Inicio de sesión" in content and "Intento de acceso fallido" in content
    assert "auth.login" in content


def test_entries_are_readable_with_links_to_what_still_exists(client, superadmin, alice, photo):
    face = photo.faces.first()
    voting.create_proposal(alice, photo, Proposal.Kind.FACE_IDENTITY, face=face, person_name="Ana")
    audit.log(alice, "photo.rotate", photo, degrees=90)
    AuditLog.objects.create(user=alice, action="something.new", details={"x": 1})
    client.force_login(superadmin)

    content = client.get(reverse("audit"), {"user": alice.pk}).content.decode()

    assert "Propuesta creada" in content and "«Ana» en la foto" in content
    assert "Foto girada" in content and reverse("photo_detail", args=[photo.pk]) in content
    assert "something.new" in content  # unknown actions are never hidden
    assert "<dt>degrees</dt><dd>90</dd>" in content


def test_filters_by_event_category_and_period(client, superadmin, alice, bob, photo):
    audit.log(alice, "photo.rotate", photo, degrees=90)
    audit.log(bob, "photo.crop", photo)
    old = audit.log(alice, "export.request")
    AuditLog.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=40))
    client.force_login(superadmin)

    by_event = listed(client.get(reverse("audit"), {"event": "photo.rotate"}))
    assert "Foto girada" in by_event and "Foto recortada" not in by_event

    by_category = listed(client.get(reverse("audit"), {"event": "cat:photos"}))
    assert "Foto girada" in by_category and "Foto recortada" in by_category
    assert "Exportación pedida" not in by_category

    recent = listed(client.get(reverse("audit"), {"period": "30d"}))
    assert "Exportación pedida" not in recent
    everything = listed(client.get(reverse("audit")))
    assert "Exportación pedida" in everything

    by_user = listed(client.get(reverse("audit"), {"user": bob.pk}))
    assert "Foto recortada" in by_user and "Foto girada" not in by_user


def test_invalid_filters_are_ignored(client, superadmin):
    client.force_login(superadmin)
    response = client.get(reverse("audit"), {"user": "x", "event": "nope", "period": "?"})
    assert response.status_code == 200


def test_summary_and_tabs_of_a_person(client, superadmin, alice, bob, photo):
    face = photo.faces.first()
    proposal = voting.create_proposal(
        bob, photo, Proposal.Kind.FACE_IDENTITY, face=face, person_name="Ana"
    )
    voting.cast_vote(alice, proposal, Vote.DOWN)
    voting.create_proposal(alice, photo, Proposal.Kind.EVENT, event_name="Boda")
    client.force_login(superadmin)

    content = client.get(reverse("audit"), {"user": alice.pk}).content.decode()
    assert "@alice" in content and "Propuestas" in content and "👎 1" in content

    proposals = client.get(reverse("audit"), {"user": alice.pk, "tab": "proposals"})
    assert "Boda" in proposals.content.decode()
    votes = client.get(reverse("audit"), {"user": alice.pk, "tab": "votes"}).content.decode()
    assert "👎" in votes and "Ana" in votes


def test_pagination_keeps_the_filters(client, superadmin, alice, photo):
    for _ in range(55):
        audit.log(alice, "photo.rotate", photo, degrees=90)
    client.force_login(superadmin)
    content = client.get(
        reverse("audit"), {"user": alice.pk, "event": "photo.rotate"}
    ).content.decode()
    assert "Página 1 de 2" in content
    assert f"user={alice.pk}&amp;event=photo.rotate&amp;page=2" in content


def test_user_list_links_to_each_activity(client, superadmin, alice):
    client.force_login(superadmin)
    content = client.get(reverse("user_list")).content.decode()
    assert f"{reverse('audit')}?user={alice.pk}" in content
    content = client.get(reverse("admin_home")).content.decode()
    assert reverse("audit") in content


def test_targets_that_no_longer_exist_are_named_from_the_details(client, superadmin, alice):
    AuditLog.objects.create(
        user=alice,
        action="scan.delete",
        target_type="catalog.scan",
        target_id="999",
        details={"file": "scan-viejo.png", "photos": 4},
    )
    AuditLog.objects.create(
        user=alice, action="photo.rotate", target_type="catalog.photo", target_id="998"
    )
    client.force_login(superadmin)
    content = listed(client.get(reverse("audit")))
    assert "scan-viejo.png (ya no existe)" in content
    assert "Foto 998 (ya no existe)" in content


@pytest.fixture
def alice_proposals(alice, photo, boda):
    voting.create_proposal(alice, photo, Proposal.Kind.EVENT, event_type=boda)
    voting.create_proposal(
        alice, photo, Proposal.Kind.DATE, value=voting.normalize_date("year", 1985)
    )


def proposals_tab(client, user, **filters):
    params = {"user": user.pk, "tab": "proposals", **filters}
    return client.get(reverse("audit"), params).content.decode()


def test_proposals_tab_has_the_gallery_filter_bar(client, superadmin, alice, alice_proposals):
    client.force_login(superadmin)
    content = proposals_tab(client, alice)
    assert 'class="filter-bar"' in content
    assert 'data-open-sheet="sheet-status"' in content and 'id="sheet-kind"' in content
    assert "2 propuestas" in content


def test_proposals_tab_filters_by_kind_and_status(client, superadmin, alice, alice_proposals):
    client.force_login(superadmin)
    content = proposals_tab(client, alice, kind="event")
    assert "1 propuesta" in content and "Boda" in content and "1985" not in content
    assert "Ninguna propuesta con estos filtros." in proposals_tab(client, alice, status="rejected")
    assert "2 propuestas" in proposals_tab(client, alice, kind="x", status="y")
