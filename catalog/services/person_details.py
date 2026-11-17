"""Honorific and nicknames of a person (spec section 5.13).

Any collaborator sets them directly, without a vote; every change is audited.

- Honorific: at most one, written before the name ("Tito" + "Paco"). Empty removes it.
- Nicknames: a list, no two equal ones (ignoring accents and case) on the same person.
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext as _

from accounts.services import audit
from catalog.models import Person, PersonNickname
from catalog.services.names import matches, name_key, normalize_label


def normalize_honorific(text):
    """'tito' → 'Tito' (first letter in upper case, the rest as typed)."""
    return normalize_label(text)[:50]


def normalize_nickname(text):
    return " ".join(str(text).split())[:100]


def _locked_person(person):
    person = Person.objects.select_for_update().get(pk=person.pk)
    if person.merged_into_id or person.is_provisional:
        raise ValidationError(_("Solo se puede hacer con personas ya aceptadas en el álbum."))
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
def add_nickname(user, person, text):
    person = _locked_person(person)
    text = normalize_nickname(text)
    if not text:
        raise ValidationError(_("Escribe el apodo."))
    key = name_key(text)
    if person.nicknames.filter(text_key=key).exists():
        raise ValidationError(_("Ese apodo ya está guardado."))
    nickname = PersonNickname.objects.create(
        person=person, text=text, text_key=key, created_by=user
    )
    audit.log(user, "person.nickname_add", person, nickname=text)
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
    target_keys = set(target.nicknames.values_list("text_key", flat=True))
    for nickname in source.nicknames.all():
        if nickname.text_key in target_keys:
            nickname.delete()
        else:
            nickname.person = target
            nickname.save(update_fields=["person"])
            target_keys.add(nickname.text_key)


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
