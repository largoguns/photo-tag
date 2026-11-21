import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import translation

from accounts.models import AuditLog
from catalog.models import Person, Relationship
from catalog.services import relationships, voting
from catalog.services.persons import merge_persons

pytestmark = pytest.mark.django_db

K = Relationship.Kind


@pytest.fixture(autouse=True)
def spanish():
    """Labels and messages are checked in Spanish whatever an earlier test activated."""
    with translation.override("es"):
        yield


def person(name):
    return Person.objects.create(display_name=name)


@pytest.fixture
def ana():
    return person("Ana")


@pytest.fixture
def luis():
    return person("Luis")


def test_added_directly_in_canonical_form_and_audited(alice, ana, luis):
    relationship = relationships.add(alice, ana, "child", other=luis)  # Luis is Ana's child

    assert (relationship.person_from, relationship.person_to) == (ana, luis)
    assert relationship.kind == K.PARENT and relationship.created_by == alice
    assert str(relationship) == "Ana, progenitor de Luis"
    entry = AuditLog.objects.get(action="relationship.add")
    assert entry.details["label"] == "Ana, progenitor de Luis"


def test_symmetric_kinds_are_stored_once(alice, ana, luis):
    relationships.add(alice, luis, "cousin", other=ana)
    with pytest.raises(ValidationError, match="ya está guardada"):
        relationships.add(alice, ana, "cousin", other=luis)
    assert Relationship.objects.get().person_from == ana  # lower id first


def test_a_family_field_is_replaced(alice, ana, luis):
    relationships.add(alice, ana, "partner", other=luis)
    relationships.add(alice, luis, "ex_partner", other=ana)

    assert Relationship.objects.get().kind == K.EX_PARTNER
    entry = AuditLog.objects.filter(action="relationship.add").latest("pk")
    assert entry.details["replaced"] == ["Ana y Luis: pareja"]


def test_social_relationships_add_up(alice, ana, luis):
    relationships.add(alice, ana, "cousin", other=luis)
    relationships.add(alice, ana, "coworker", other=luis)
    relationships.add(alice, ana, "neighbor", other=luis)

    assert set(Relationship.objects.values_list("kind", flat=True)) == {
        K.COUSIN,
        K.COWORKER,
        K.NEIGHBOR,
    }


def test_activity_is_required_normalized_and_can_be_changed(alice, ana, luis):
    with pytest.raises(ValidationError, match="actividad"):
        relationships.add(alice, ana, "activity_mate", other=luis)

    relationships.add(alice, ana, "activity_mate", other=luis, activity="  FÚTBOL ")
    assert Relationship.objects.get().activity == "Fútbol"
    with pytest.raises(ValidationError, match="ya está guardada"):
        relationships.add(alice, luis, "activity_mate", other=ana, activity="futbol")
    relationships.add(alice, ana, "activity_mate", other=luis, activity="pádel")
    assert Relationship.objects.get().activity == "Pádel"


def test_remove(alice, ana, luis):
    relationship = relationships.add(alice, ana, "friend", other=luis)
    relationships.remove(alice, relationship)

    assert not Relationship.objects.exists()
    entry = AuditLog.objects.get(action="relationship.delete")
    assert entry.details["label"] == "Ana y Luis: amigo"


def test_unknown_name_creates_the_person(alice, ana):
    relationships.add(alice, ana, "grandparent", other_name="bisabuelo pepe")
    pepe = Person.objects.get(display_name="Bisabuelo Pepe")
    assert not pepe.is_provisional
    assert Relationship.objects.get().person_from == pepe


def test_an_invalid_relationship_does_not_create_the_person(alice, ana):
    relationships.add(alice, ana, "parent", other=person("Madre"))
    relationships.add(alice, ana, "parent", other=person("Padre"))
    with pytest.raises(ValidationError, match="dos progenitores"):
        relationships.add(alice, ana, "parent", other_name="Nadie")
    assert not Person.objects.filter(display_name="Nadie").exists()


def test_provisional_person_is_kept_while_related(ana):
    pepe = Person.objects.create(display_name="Pepe", is_provisional=True)
    Relationship.objects.create(person_from=ana, person_to=pepe, kind=K.FRIEND)
    voting.cleanup_provisional_person(pepe)
    assert Person.objects.filter(pk=pepe.pk).exists()


def test_cannot_relate_a_person_with_oneself(alice, ana):
    with pytest.raises(ValidationError, match="consigo misma"):
        relationships.add(alice, ana, "friend", other=ana)


def test_unknown_role(alice, ana, luis):
    with pytest.raises(ValidationError):
        relationships.add(alice, ana, "boss", other=luis)


def test_no_ancestry_cycles(alice, ana, luis):
    relationships.add(alice, luis, "parent", other=ana)  # Ana → Luis
    nieto = person("Nieto")
    relationships.add(alice, nieto, "parent", other=luis)  # Luis → Nieto

    with pytest.raises(ValidationError, match="antepasado"):
        relationships.add(alice, ana, "grandparent", other=nieto)


def test_deduced_relationships_cannot_be_added_or_contradicted(alice, ana, luis):
    marta = person("Marta")
    relationships.add(alice, luis, "parent", other=ana)
    relationships.add(alice, marta, "parent", other=ana)

    with pytest.raises(ValidationError, match="ya se deduce"):
        relationships.add(alice, luis, "sibling", other=marta)
    with pytest.raises(ValidationError, match="deduce: hermano"):
        relationships.add(alice, luis, "cousin", other=marta)


def test_explicit_relationships_without_the_intermediate_link(alice, ana, luis):
    # A parent-in-law without the partner in the album is still allowed.
    assert relationships.add(alice, ana, "parent_in_law", other=luis).kind == K.PARENT_IN_LAW


def test_merge_moves_relationships_recanonicalizing_and_dropping_duplicates(admin_user, alice):
    a, b, c = person("A"), person("B"), person("C")  # ids ascending
    relationships.add(alice, b, "friend", other=c)  # (b, c)
    relationships.add(alice, a, "friend", other=c)  # (a, c)
    relationships.add(alice, b, "neighbor", other=a)  # (a, b) → (a, c) after the merge

    merge_persons(admin_user, b, c)

    rows = set(Relationship.objects.values_list("person_from", "person_to", "kind"))
    # (b, c) friends became (c, c): dropped. The neighbours keep the canonical order.
    assert rows == {(a.pk, c.pk, K.FRIEND), (a.pk, c.pk, K.NEIGHBOR)}


def test_merge_keeps_the_targets_family_relationship(admin_user, alice, ana, luis):
    twin = person("Luis bis")
    relationships.add(alice, ana, "sibling", other=luis)
    relationships.add(alice, ana, "cousin", other=twin)

    merge_persons(admin_user, twin, luis)

    assert list(Relationship.objects.values_list("kind", flat=True)) == [K.SIBLING]


def test_database_enforces_the_canonical_form(ana, luis):
    low, high = sorted((ana, luis), key=lambda p: p.pk)
    with pytest.raises(IntegrityError), transaction.atomic():
        Relationship.objects.create(person_from=high, person_to=low, kind=K.SIBLING)
    with pytest.raises(IntegrityError), transaction.atomic():
        Relationship.objects.create(person_from=ana, person_to=ana, kind=K.PARENT)
    Relationship.objects.create(person_from=low, person_to=high, kind=K.FRIEND)
    with pytest.raises(IntegrityError), transaction.atomic():
        Relationship.objects.create(person_from=low, person_to=high, kind=K.FRIEND)
    # Asymmetric kinds may go either way.
    Relationship.objects.create(person_from=high, person_to=low, kind=K.PARENT)


# ---------------------------------------------------------------------------
# Person page
# ---------------------------------------------------------------------------


def test_person_page_keeps_photos_first_and_relationships_in_the_menu(client, alice, ana, luis):
    relationships.add(alice, ana, "child", other=luis)
    client.force_login(alice)

    content = client.get(reverse("person_detail", args=[ana.pk])).content.decode()

    assert 'data-open-sheet="sheet-family"' in content and 'id="sheet-family"' in content
    assert 'data-open-sheet="sheet-nicknames"' in content
    assert '<span class="menu-count">1</span>' in content
    sheet = content.split('id="sheet-family"')[1].split("</dialog>")[0]
    assert "Luis" in sheet and "Hijo" in sheet
    assert 'id="people-data"' in content


def test_add_and_remove_from_the_person_page(client, alice, ana, luis):
    client.force_login(alice)
    url = reverse("person_detail", args=[ana.pk])

    response = client.post(
        reverse("add_relationship", args=[ana.pk]),
        {"name": "Luis", "role": "sibling", "next": url},
    )
    assert response.url == f"{url}?sheet=family"
    relationship = Relationship.objects.get()

    # The page opens again the sheet it came from.
    content = client.get(response.url).content.decode()
    assert 'id="sheet-family" aria-labelledby="sheet-family-title" data-autoopen' in content

    client.post(reverse("remove_relationship", args=[relationship.pk]), {"next": url})
    assert not Relationship.objects.exists()


def test_errors_are_shown_on_the_person_page(client, alice, ana):
    client.force_login(alice)
    response = client.post(
        reverse("add_relationship", args=[ana.pk]), {"name": "Ana", "role": "friend"}, follow=True
    )
    assert "consigo misma" in response.content.decode()


def test_namesakes_from_the_person_page(client, alice, ana, luis):
    other_luis = person("Luis")
    client.force_login(alice)
    url = reverse("add_relationship", args=[ana.pk])

    response = client.post(url, {"name": "Luis", "role": "sibling"}, follow=True)
    assert "varias personas llamadas «Luis»" in response.content.decode()
    assert not Relationship.objects.exists()

    client.post(url, {"name": "Luis", "person_id": other_luis.pk, "role": "sibling"})
    relationship = Relationship.objects.get()
    assert other_luis.pk in (relationship.person_from_id, relationship.person_to_id)
    assert luis.pk not in (relationship.person_from_id, relationship.person_to_id)

    client.post(url, {"name": "Luis", "new_person": "1", "role": "cousin"})
    assert Person.objects.filter(display_name="Luis").count() == 3


def test_login_is_required(client, ana):
    response = client.post(reverse("add_relationship", args=[ana.pk]), {"role": "friend"})
    assert response.status_code == 302 and "login" in response.url
