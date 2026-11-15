import pytest

from catalog.services.names import matches, name_key, normalize_name, search


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("adri", "Adri"),
        ("  maría   josé  ", "María José"),
        ("maría josé DE LA cruz", "María José de la Cruz"),
        ("de la vega", "De la Vega"),
        ("juan y pedro", "Juan y Pedro"),
        ("ADRIÁN", "Adrián"),
        ("maría-josé", "María-José"),
        ("o'neill", "O'Neill"),
        ("Leonardo DiCaprio", "Leonardo DiCaprio"),
        ("ñoño", "Ñoño"),
        ("", ""),
    ],
)
def test_normalize_name(typed, expected):
    assert normalize_name(typed) == expected


def test_name_key_ignores_case_accents_and_spaces():
    assert name_key("Adrián  López") == name_key("adrian lopez") == "adrian lopez"
    assert name_key("Luis") != name_key("Luisa")


@pytest.mark.parametrize(
    ("query", "text", "expected"),
    [
        ("ari", "María", True),
        ("MARIA", "María", True),
        ("maría", "Maria", True),
        ("lop mar", "María López", True),
        ("  jose  ", "José Luis", True),
        ("xyz", "María", False),
        ("mar xyz", "María López", False),
    ],
)
def test_loose_matching(query, text, expected):
    assert matches(query, text) is expected


@pytest.mark.django_db
def test_search_filters_a_queryset():
    from catalog.models import Person

    maria = Person.objects.create(display_name="María López")
    Person.objects.create(display_name="Pedro")
    assert list(search(Person.objects.all(), "ari", "display_name")) == [maria]
    assert search(Person.objects.all(), "  ", "display_name").count() == 2
