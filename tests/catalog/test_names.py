import pytest

from catalog.services.names import name_key, normalize_name


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
