"""Honorific and nicknames of a person (spec section 5.13).

Any collaborator sets them directly, without a vote; every change is audited.

- Honorific: at most one, written before the name ("Tito" + "Paco"). Empty removes it.
- Nicknames: a list, no two equal ones (ignoring accents and case) on the same person.
  Each one may carry 1 to 3 emojis that stand for it (quiz «Emojis», spec 5.14).
"""

import unicodedata

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext as _

from accounts.services import audit
from catalog.models import Person, PersonNickname
from catalog.services.names import matches, name_key, normalize_label, split_honorific


def normalize_honorific(text):
    """'tito' → 'Tito' (first letter in upper case, the rest as typed)."""
    return normalize_label(text)[:50]


def normalize_nickname(text):
    return " ".join(str(text).split())[:100]


MAX_EMOJIS = 3
ZWJ = "\u200d"
# Joined to the emoji before them: variation selectors, keycap, skin tones, tags.
EMOJI_MODIFIERS = {"\ufe0e", "\ufe0f", "\u20e3", *map(chr, range(0x1F3FB, 0x1F400))}
EMOJI_MODIFIERS |= set(map(chr, range(0xE0020, 0xE0080)))
REGIONAL = range(0x1F1E6, 0x1F200)  # two of them make a flag


def normalize_emoji(text):
    """Spaces removed; ValidationError unless it is 1 to 3 emojis (empty is allowed: it
    removes the emoji). Counts what is seen as one emoji: «👨‍👩‍👧», «👍🏽» or «🇪🇸»."""
    text = "".join(str(text).split())
    count, joined, flag_half = 0, False, False
    for char in text:
        if char == ZWJ:
            joined = True
            continue
        if char in EMOJI_MODIFIERS:
            continue
        if ord(char) in REGIONAL:
            flag_half = not flag_half
            count += 0 if (joined or not flag_half) else 1
        elif unicodedata.category(char) == "So":
            count += 0 if joined else 1
            flag_half = False
        else:
            raise ValidationError(_("Solo se pueden poner emojis (de 1 a 3)."))
        joined = False
    if count > MAX_EMOJIS or len(text) > 32:
        raise ValidationError(_("Solo se pueden poner emojis (de 1 a 3)."))
    return text


def _locked_person(person):
    person = Person.objects.select_for_update().get(pk=person.pk)
    if person.merged_into_id or person.is_provisional:
        raise ValidationError(_("Solo se puede hacer con personas ya aceptadas en el álbum."))
    return person


@transaction.atomic
def set_pet(user, person, is_pet):
    """Mark a person of the album as a pet, or back as a person (spec 5.16). Direct and
    audited. A pet has no family, no linked user and no face suggestions; its photos are
    exported again (its regions become Type=Pet)."""
    from catalog.models import Face, Photo, Relationship

    person = _locked_person(person)
    if person.is_pet == is_pet:
        return person
    relationships = Relationship.objects.filter(Q(person_from=person) | Q(person_to=person))
    if is_pet:
        if person.user_id:
            raise ValidationError(
                _("Hay un usuario identificado como esta persona: que pulse «No soy yo» antes.")
            )
        if relationships.exclude(kind=Relationship.Kind.OWNER).exists():
            raise ValidationError(
                _("Tiene relaciones de persona (familia, amigos…): quítalas antes.")
            )
    elif relationships.filter(kind=Relationship.Kind.OWNER).exists():
        raise ValidationError(_("Tiene relaciones «Mascota de»: quítalas antes."))
    person.is_pet = is_pet
    person.save(update_fields=["is_pet"])
    photo_ids = Face.objects.filter(person=person).values_list("photo_id", flat=True)
    Photo.objects.filter(pk__in=set(photo_ids)).update(catalog_changed_at=timezone.now())
    audit.log(user, "person.pet", person, is_pet=is_pet)
    return person


@transaction.atomic
def set_honorific(user, person, text):
    person = _locked_person(person)
    text = normalize_honorific(text)
    old = person.honorific
    if text == old:
        return person
    person.honorific = text
    person.save(update_fields=["honorific"])
    audit.log(user, "person.honorific", person, old=old, new=text)
    return person


@transaction.atomic
def add_nickname(user, person, text, emoji=""):
    person = _locked_person(person)
    text = normalize_nickname(text)
    if not text:
        raise ValidationError(_("Escribe el apodo."))
    emoji = normalize_emoji(emoji)
    key = name_key(text)
    if person.nicknames.filter(text_key=key).exists():
        raise ValidationError(_("Ese apodo ya está guardado."))
    nickname = PersonNickname.objects.create(
        person=person, text=text, text_key=key, emoji=emoji, created_by=user
    )
    audit.log(user, "person.nickname_add", person, nickname=text, emoji=emoji)
    return nickname


@transaction.atomic
def set_nickname_emoji(user, nickname, emoji):
    """Put, change or (empty) remove the emojis of a nickname. Direct and audited."""
    person = _locked_person(nickname.person)
    emoji = normalize_emoji(emoji)
    nickname = PersonNickname.objects.get(pk=nickname.pk)
    old = nickname.emoji
    if emoji == old:
        return nickname
    nickname.emoji = emoji
    nickname.save(update_fields=["emoji"])
    audit.log(user, "person.nickname_emoji", person, nickname=nickname.text, old=old, new=emoji)
    return nickname


@transaction.atomic
def remove_nickname(user, nickname):
    person = _locked_person(nickname.person)
    audit.log(user, "person.nickname_remove", person, nickname=nickname.text)
    nickname.delete()


def move_to(source, target):
    """Merge the details of `source` into `target` (the caller saves `target`): the target
    keeps its honorific (or takes the source's if it has none) and gets the nicknames of
    both without duplicates."""
    if not target.honorific and source.honorific:
        target.honorific = source.honorific
    target_nicknames = {n.text_key: n for n in target.nicknames.all()}
    target_keys = set(target_nicknames)
    for nickname in source.nicknames.all():
        if nickname.text_key in target_keys:
            # The same nickname on both: the target keeps its emoji, or takes this one.
            same = target_nicknames.get(nickname.text_key)
            if same and not same.emoji and nickname.emoji:
                same.emoji = nickname.emoji
                same.save(update_fields=["emoji"])
            nickname.delete()
        else:
            nickname.person = target
            nickname.save(update_fields=["person"])
            target_keys.add(nickname.text_key)


# ---------------------------------------------------------------------------
# Forms of address written inside the name ("Abuela Ana"): administration tool
# ---------------------------------------------------------------------------


def honorifics_in_names():
    """[(person, honorific, name)] of accepted people whose name starts with a form of
    address that belongs in the honorific. Skipped when they already have a different
    honorific (an administrator decides that one by hand)."""
    result = []
    people = Person.objects.active().filter(is_provisional=False).order_by("display_name", "id")
    for person in people:
        honorific, rest = split_honorific(person.display_name)
        if not honorific:
            continue
        if person.honorific and name_key(person.honorific) != name_key(honorific):
            continue
        result.append((person, normalize_honorific(honorific), rest))
    return result


@transaction.atomic
def separate_honorific(admin, person):
    """'Abuela Ana' → honorific 'Abuela' + name 'Ana' (or only the name, when the
    honorific was already set: 'Abuela Abuela Ana'). Audited and photos re-exported."""
    from catalog.services.persons import rename_person

    person = Person.objects.get(pk=person.pk)
    honorific, rest = split_honorific(person.display_name)
    if not honorific:
        return person
    if not person.honorific:
        person = set_honorific(admin, person, honorific)
    elif name_key(person.honorific) != name_key(honorific):
        raise ValidationError(_("Ya tiene otro tratamiento: cámbialo a mano."))
    return rename_person(admin, person, rest)


def search_persons(persons, query):
    """[(person, nickname it matched by or None)] of `persons` matching the loose `query`
    by name, honorific or nickname (section 5.5)."""
    persons = list(persons)
    if not query:
        return [(person, None) for person in persons]
    nicknames = {}
    for person_id, text in PersonNickname.objects.filter(
        person__in=[p.pk for p in persons]
    ).values_list("person_id", "text"):
        nicknames.setdefault(person_id, []).append(text)
    result = []
    for person in persons:
        if matches(query, person.display_name, person.honorific):
            result.append((person, None))
            continue
        nickname = next((t for t in nicknames.get(person.pk, []) if matches(query, t)), None)
        if nickname:
            result.append((person, nickname))
    return result
