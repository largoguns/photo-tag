"""Person name normalization: consistent capitalization and accent-insensitive matching."""

import re
import unicodedata

# Lower-case unless they start the name: "María José de la Cruz".
PARTICLES = {"de", "del", "la", "las", "los", "y", "e"}
_SEPARATORS = re.compile(r"([-'’])")


def _capitalize(word):
    if not word:
        return word
    letters = [c for c in word if c.isalpha()]
    # Keep deliberate inner capitals ("DiCaprio"); normalize ALL CAPS and lower case.
    mixed = any(c.isupper() for c in letters[1:]) and not all(c.isupper() for c in letters)
    rest = word[1:] if mixed else word[1:].lower()
    return word[0].upper() + rest


def normalize_name(name):
    """'  maría josé DE LA cruz ' → 'María José de la Cruz'."""
    words = " ".join(str(name).split()).split(" ")
    result = []
    for index, word in enumerate(words):
        if index > 0 and word.lower() in PARTICLES:
            result.append(word.lower())
            continue
        parts = _SEPARATORS.split(word)  # keeps the separators
        result.append(
            "".join(_capitalize(part) if i % 2 == 0 else part for i, part in enumerate(parts))
        )
    return " ".join(result).strip()


def name_key(name):
    """Comparison key: no accents, case-folded, single spaces ('Adrián' == 'adrian')."""
    decomposed = unicodedata.normalize("NFKD", " ".join(str(name).split()))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def normalize_label(text):
    """Sentence case for labels such as event types: 'fin de CURSO' → 'Fin de CURSO'.

    Only the first letter is forced to upper case; text typed ALL IN CAPS is lowered first.
    """
    text = " ".join(str(text).split())
    if not text:
        return ""
    letters = [c for c in text if c.isalpha()]
    if letters and all(c.isupper() for c in letters):
        text = text.lower()
    return text[0].upper() + text[1:]


def matches(query, *texts):
    """Loose search: every word of `query` appears somewhere in `texts`, ignoring accents
    and case ("ari" finds "María", "lop mar" finds "María López")."""
    words = name_key(query).split()
    haystack = " ".join(name_key(text) for text in texts if text)
    return all(word in haystack for word in words)


def search(queryset, query, *fields):
    """Filter `queryset` with `matches` over `fields`.

    Done in Python because SQLite and PostgreSQL disagree on accent-insensitive matching;
    people and users are a few hundred rows at most.
    """
    if not name_key(query):
        return queryset
    rows = queryset.values_list("pk", *fields)
    return queryset.filter(pk__in=[pk for pk, *values in rows if matches(query, *values)])
