import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import translation

from accounts.models import AuditLog
from catalog.models import Person, PersonNickname
from catalog.services import person_details
from catalog.services.persons import merge_persons
from exporter.services import xmp

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def spanish():
    with translation.override("es"):
        yield


@pytest.fixture
def paco():
    return Person.objects.create(display_name="Paco")


def test_honorific_is_normalized_shown_and_audited(paco, alice):
    person_details.set_honorific(alice, paco, "  tito ")
    paco.refresh_from_db()

    assert paco.honorific == "Tito"
    assert paco.shown_name == "Tito Paco" and str(paco) == "Tito Paco"
    assert paco.display_name == "Paco"
    entry = AuditLog.objects.get(action="person.honorific")
    assert entry.details == {"old": "", "new": "Tito"}


def test_honorific_is_replaced_and_removed(paco, alice):
    person_details.set_honorific(alice, paco, "Tito")
    person_details.set_honorific(alice, paco, "Tío")
    person_details.set_honorific(alice, paco, "")
    paco.refresh_from_db()
    assert paco.honorific == "" and paco.shown_name == "Paco"
    # Saving the same value records nothing.
    person_details.set_honorific(alice, paco, "")
    assert AuditLog.objects.filter(action="person.honorific").count() == 3


def test_nicknames_are_added_and_removed(paco, alice):
    nickname = person_details.add_nickname(alice, paco, "  Pico   pata ")
    assert nickname.text == "Pico pata" and nickname.created_by == alice
    with pytest.raises(ValidationError, match="ya está guardado"):
        person_details.add_nickname(alice, paco, "PICO PATA")
    person_details.add_nickname(alice, paco, "Pico Paco")
    assert paco.nicknames.count() == 2

    person_details.remove_nickname(alice, nickname)
    assert list(paco.nicknames.values_list("text", flat=True)) == ["Pico Paco"]
    actions = list(AuditLog.objects.values_list("action", flat=True))
    assert actions.count("person.nickname_add") == 2 and "person.nickname_remove" in actions


def test_the_same_nickname_can_belong_to_different_people(paco, alice):
    pepe = Person.objects.create(display_name="Pepe")
    person_details.add_nickname(alice, paco, "Pico")
    person_details.add_nickname(alice, pepe, "Pico")
    assert PersonNickname.objects.count() == 2


def test_only_for_active_accepted_people(paco, alice):
    provisional = Person.objects.create(display_name="Nuevo", is_provisional=True)
    merged = Person.objects.create(display_name="Viejo", merged_into=paco)
    for person in (provisional, merged):
        with pytest.raises(ValidationError, match="aceptadas"):
            person_details.set_honorific(alice, person, "Tito")
    with pytest.raises(ValidationError, match="Escribe el apodo"):
        person_details.add_nickname(alice, paco, "   ")


def test_merge_keeps_target_honorific_and_joins_nicknames(paco, alice, admin_user):
    other = Person.objects.create(display_name="Francisco")
    person_details.set_honorific(alice, other, "Tío")
    person_details.add_nickname(alice, paco, "Pico")
    person_details.add_nickname(alice, other, "pico")
    person_details.add_nickname(alice, other, "Paquete")

    merge_persons(admin_user, other, paco)

    paco.refresh_from_db()
    assert paco.honorific == "Tío"  # the target had none
    assert sorted(paco.nicknames.values_list("text", flat=True)) == ["Paquete", "Pico"]


def test_merge_keeps_the_targets_own_honorific(paco, alice, admin_user):
    other = Person.objects.create(display_name="Francisco")
    person_details.set_honorific(alice, paco, "Tito")
    person_details.set_honorific(alice, other, "Tío")
    merge_persons(admin_user, other, paco)
    paco.refresh_from_db()
    assert paco.honorific == "Tito"


def test_search_by_honorific_and_nickname(paco, alice):
    person_details.set_honorific(alice, paco, "Tito")
    person_details.add_nickname(alice, paco, "Pico pata")
    paco.refresh_from_db()
    people = Person.objects.all()

    assert person_details.search_persons(people, "tito paco") == [(paco, None)]
    assert person_details.search_persons(people, "pata pico") == [(paco, "Pico pata")]
    assert person_details.search_persons(people, "nadie") == []


def test_persons_list_shows_shown_name_and_nickname_hint(client, paco, alice):
    person_details.set_honorific(alice, paco, "Tito")
    person_details.add_nickname(alice, paco, "Pico pata")
    client.force_login(alice)

    content = client.get(reverse("persons"), {"q": "pico"}).content.decode()

    assert "Tito Paco" in content and "· Pico pata" in content


def test_gallery_person_sheet_searches_nicknames(client, paco, alice):
    person_details.add_nickname(alice, paco, "Pico pata")
    client.force_login(alice)
    content = client.get(reverse("gallery")).content.decode()
    assert 'data-option-label="Paco Pico pata"' in content


def test_export_keeps_the_plain_name(photo, paco, alice):
    person_details.set_honorific(alice, paco, "Tito")
    face = photo.faces.first()
    face.person = paco
    face.save()
    assert [r["name"] for r in xmp.face_regions(photo)] == ["Paco"]


def test_photo_autocomplete_sends_the_plain_name(client, photo, paco, alice):
    person_details.set_honorific(alice, paco, "Tito")
    person_details.add_nickname(alice, paco, "Pico")
    client.force_login(alice)

    people = client.get(reverse("photo_detail", args=[photo.pk])).context["people_json"]

    entry = next(p for p in people if p["name"] == "Paco")
    assert entry["shown"] == "Tito Paco" and entry["nicknames"] == ["Pico"]


def test_person_page_sets_honorific_and_nicknames(client, paco, alice):
    client.force_login(alice)
    url = reverse("person_detail", args=[paco.pk])

    client.post(reverse("set_honorific", args=[paco.pk]), {"honorific": "tito", "next": url})
    response = client.post(
        reverse("add_nickname", args=[paco.pk]), {"nickname": "Pico", "next": url}
    )
    assert response.url == f"{url}?sheet=nicknames"

    content = client.get(url).content.decode()
    assert "<h1>Tito Paco</h1>" in content
    sheet = content.split('id="sheet-nicknames"')[1].split("</dialog>")[0]
    assert "Pico" in sheet
    # Nicknames are under the name, as chips that open the sheet; not in the ⋮ menu.
    header = content.split('class="chips person-nicknames"')[1].split("</ul>")[0]
    assert ">Pico</button>" in header and 'data-sheet-focus="nickname-text"' in header
    menu = content.split('class="user-menu person-menu"')[1].split("</details>")[0]
    assert "sheet-nicknames" not in menu

    nickname = PersonNickname.objects.get()
    client.post(reverse("remove_nickname", args=[nickname.pk]), {"next": url})
    assert not PersonNickname.objects.exists()

    response = client.post(reverse("add_nickname", args=[paco.pk]), {"nickname": " "}, follow=True)
    assert "Escribe el apodo" in response.content.decode()


# --- Forms of address inside the name ("Abuela Ana") ----------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Abuela Ana", ("Abuela", "Ana")),
        ("TÍA Lola", ("TÍA", "Lola")),
        ("Tito Paco Martín", ("Tito", "Paco Martín")),
        ("Abuela", ("", "Abuela")),  # nothing would be left of the name
        ("Ana Abuela", ("", "Ana Abuela")),
        ("Titanic", ("", "Titanic")),
    ],
)
def test_split_honorific(name, expected):
    from catalog.services.names import split_honorific

    assert split_honorific(name) == expected


def test_a_typed_form_of_address_goes_to_the_honorific(alice):
    from catalog.services import voting

    person = voting.resolve_person("abuela ana", alice)
    assert (person.honorific, person.display_name, person.shown_name) == (
        "Abuela",
        "Ana",
        "Abuela Ana",
    )
    # Typing the shown name finds that same person instead of creating another one.
    assert voting.resolve_person("Abuela Ana", alice) == person


def test_admin_separates_honorifics_written_in_the_name(client, admin_user, alice):
    grandma = Person.objects.create(display_name="Abuela Ana")
    doubled = Person.objects.create(display_name="Abuela Isabel", honorific="Abuela")
    other = Person.objects.create(display_name="Tito Paco", honorific="Padrino")  # decided by hand
    Person.objects.create(display_name="Ana")
    url = reverse("admin_honorifics")

    client.force_login(alice)
    assert client.get(url).status_code == 403

    client.force_login(admin_user)
    assert "2 por separar" in client.get(reverse("admin_home")).content.decode()
    content = client.get(url).content.decode()
    assert "Abuela Ana" in content and "Abuela Abuela Isabel" in content
    assert "Padrino Tito Paco" not in content

    client.post(url, {"person": grandma.pk})
    grandma.refresh_from_db()
    assert (grandma.honorific, grandma.display_name) == ("Abuela", "Ana")  # Ana already exists
    assert AuditLog.objects.filter(action="person.honorific").exists()
    assert AuditLog.objects.filter(action="person.rename").exists()

    response = client.post(url, {"person": "all"}, follow=True)
    assert "1 persona actualizada" in response.content.decode()
    doubled.refresh_from_db()
    other.refresh_from_db()
    assert (doubled.honorific, doubled.display_name) == ("Abuela", "Isabel")
    assert other.display_name == "Tito Paco"
    assert "No hay ninguna" in client.get(url).content.decode()
