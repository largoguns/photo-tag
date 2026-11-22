"""Emojis of nicknames (spec 5.13, used by the «Emojis» quiz of 5.14)."""

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import translation

from accounts.models import AuditLog
from catalog.models import Person
from catalog.services import person_details
from catalog.services.persons import merge_persons

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def spanish():
    with translation.override("es"):
        yield


@pytest.fixture
def paco():
    return Person.objects.create(display_name="Paco")


@pytest.mark.parametrize(
    ("text", "saved"),
    [
        ("🐦", "🐦"),
        (" 🐦 🦶 ", "🐦🦶"),
        ("👍🏽", "👍🏽"),  # skin tone
        ("👨‍👩‍👧‍👦🍷🎸", "👨‍👩‍👧‍👦🍷🎸"),  # a family joined with ZWJ counts as one
        ("🇪🇸🇫🇷", "🇪🇸🇫🇷"),  # two flags
        ("❤️", "❤️"),  # variation selector
        ("", ""),
    ],
)
def test_valid_emojis(text, saved):
    assert person_details.normalize_emoji(text) == saved


@pytest.mark.parametrize("text", ["Pico", "🐦a", "🐦🦶🍷🎸", "1", "🐦!"])
def test_only_one_to_three_emojis(text):
    with pytest.raises(ValidationError, match="Solo se pueden poner emojis"):
        person_details.normalize_emoji(text)


def test_emoji_is_set_changed_removed_and_audited(paco, alice):
    nickname = person_details.add_nickname(alice, paco, "Pico pata", "🐦")
    assert nickname.emoji == "🐦"
    person_details.set_nickname_emoji(alice, nickname, "🐦🦶")
    person_details.set_nickname_emoji(alice, nickname, "🐦🦶")  # same: nothing recorded
    person_details.set_nickname_emoji(alice, nickname, "")
    nickname.refresh_from_db()
    assert nickname.emoji == ""
    entries = AuditLog.objects.filter(action="person.nickname_emoji").order_by("id")
    assert [e.details["new"] for e in entries] == ["🐦🦶", ""]


def test_merge_keeps_emojis(paco, alice, admin_user):
    other = Person.objects.create(display_name="Francisco")
    person_details.add_nickname(alice, paco, "Pico")
    person_details.add_nickname(alice, other, "pico", "🐦")
    person_details.add_nickname(alice, other, "Paquete", "📦")
    merge_persons(admin_user, other, paco)
    emojis = dict(paco.nicknames.values_list("text", "emoji"))
    assert emojis == {"Pico": "🐦", "Paquete": "📦"}


def test_emoji_from_the_nicknames_sheet(client, paco, alice):
    client.force_login(alice)
    client.post(reverse("add_nickname", args=[paco.pk]), {"nickname": "Pico pata", "emoji": "🐦"})
    nickname = paco.nicknames.get()
    assert nickname.emoji == "🐦"

    page = client.get(reverse("person_detail", args=[paco.pk])).content.decode()
    assert reverse("set_nickname_emoji", args=[nickname.pk]) in page and "🐦" in page

    response = client.post(
        reverse("set_nickname_emoji", args=[nickname.pk]), {"emoji": "🦶"}, follow=True
    )
    nickname.refresh_from_db()
    assert nickname.emoji == "🦶"
    response = client.post(
        reverse("set_nickname_emoji", args=[nickname.pk]), {"emoji": "pata"}, follow=True
    )
    assert "Solo se pueden poner emojis" in response.content.decode()
    nickname.refresh_from_db()
    assert nickname.emoji == "🦶"
