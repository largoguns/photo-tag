import pytest
from django.utils import translation

from catalog.models import Person, Relationship
from catalog.services import kinship

K = Relationship.Kind


@pytest.fixture(autouse=True)
def spanish():
    """Labels and messages are checked in Spanish whatever an earlier test activated."""
    with translation.override("es"):
        yield


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("parent", (2, 1, K.PARENT)),  # the other (2) is the parent of the subject (1)
        ("child", (1, 2, K.PARENT)),
        ("grandparent", (2, 1, K.GRANDPARENT)),
        ("grandchild", (1, 2, K.GRANDPARENT)),
        ("uncle", (2, 1, K.UNCLE)),
        ("nephew", (1, 2, K.UNCLE)),
        ("parent_in_law", (2, 1, K.PARENT_IN_LAW)),
        ("child_in_law", (1, 2, K.PARENT_IN_LAW)),
        ("godparent", (2, 1, K.GODPARENT)),
        ("godchild", (1, 2, K.GODPARENT)),
        ("sibling", (1, 2, K.SIBLING)),
        ("partner", (1, 2, K.PARTNER)),
        ("activity_mate", (1, 2, K.ACTIVITY_MATE)),
    ],
)
def test_canonical_form(role, expected):
    assert kinship.canonical(1, role, 2) == expected


def test_symmetric_kinds_are_ordered_by_id():
    assert kinship.canonical(9, "cousin", 3) == (3, 9, K.COUSIN)


def test_every_kind_has_roles_labels_and_a_tree():
    for kind in K:
        assert kinship.tree_of(kind) in (kinship.FAMILY, kinship.SOCIAL)
        for viewer_is_from in (True, False):
            assert kinship.role_label(kinship.role_of_other(kind, viewer_is_from))
    assert set(kinship.FAMILY_ROLES + kinship.SOCIAL_ROLES) == set(kinship.ROLES)


def test_labels_are_reciprocal_and_neutral():
    # "A is parent of B": from B, A is "Progenitor"; from A, B is "Hijo".
    assert kinship.role_label(kinship.role_of_other(K.PARENT, False)) == "Progenitor"
    assert kinship.role_label(kinship.role_of_other(K.PARENT, True)) == "Hijo"
    assert kinship.role_label(kinship.role_of_other(K.PARENT_IN_LAW, True)) == "Hijo político"
    assert kinship.role_label(kinship.role_of_other(K.GODPARENT, True)) == "Ahijado"
    assert kinship.role_label(kinship.role_of_other(K.COUSIN, True)) == "Primo"


# Family used below (ids):
#   1 ─┬─ 2 (partners)        8 ── 9 (ex-partners)
#      ├── 3 ── 10 (partners: 10 is the child-in-law of 1 and 2)
#      │    └── 5
#      └── 4
#           └── 6
#   7: half sibling of 3 (child of 1 only)
FAMILY_ROWS = [
    (1, 2, K.PARTNER),
    (1, 3, K.PARENT),
    (2, 3, K.PARENT),
    (1, 4, K.PARENT),
    (2, 4, K.PARENT),
    (1, 7, K.PARENT),
    (3, 5, K.PARENT),
    (4, 6, K.PARENT),
    (3, 10, K.PARTNER),
    (8, 9, K.EX_PARTNER),
    (8, 3, K.PARENT_IN_LAW),  # explicit: excluded from deductions
]


@pytest.fixture
def graph():
    return kinship.Graph(FAMILY_ROWS)


def test_deduces_siblings_including_half_siblings(graph):
    deduced = graph.deduced(3)
    assert deduced[4] == "sibling"
    assert deduced[7] == "sibling"


def test_deduces_grandparents_uncles_nephews_and_cousins(graph):
    assert graph.deduced(5) == {
        1: "grandparent",
        2: "grandparent",
        4: "uncle",
        7: "uncle",
        6: "cousin",
    }
    assert graph.deduced(1)[5] == "grandchild"
    assert graph.deduced(4)[5] == "nephew"


def test_deduces_in_laws(graph):
    assert graph.deduced(10)[1] == "parent_in_law"
    assert graph.deduced(10)[4] == "sibling_in_law"
    assert graph.deduced(1)[10] == "child_in_law"
    assert graph.deduced(4)[10] == "sibling_in_law"


def test_ex_partners_do_not_make_in_laws():
    graph = kinship.Graph([(1, 2, K.EX_PARTNER), (3, 2, K.PARENT)])
    assert graph.deduced(1) == {}


def test_explicit_family_relationships_are_not_deduced(graph):
    assert 8 not in graph.deduced(3)
    assert 3 not in graph.deduced(8)
    assert 2 not in graph.deduced(1)  # partners: explicit


def test_explicit_siblings_count_for_uncles():
    graph = kinship.Graph([(1, 2, K.SIBLING), (1, 3, K.PARENT)])
    assert graph.deduced(3) == {2: "uncle"}


def test_ancestors_follow_parents_and_grandparents(graph):
    graph = kinship.Graph([*FAMILY_ROWS, (11, 1, K.GRANDPARENT)])
    assert graph.ancestors(5) == {3, 1, 2, 11}
    assert graph.ancestors(1) == {11}


@pytest.mark.django_db
def test_relations_for_lists_explicit_and_deduced(alice):
    a, b, c, d = (Person.objects.create(display_name=n) for n in ("Ana", "Blas", "Carla", "Dani"))
    Relationship.objects.create(person_from=a, person_to=b, kind=K.PARENT)
    Relationship.objects.create(person_from=a, person_to=c, kind=K.PARENT)
    Relationship.objects.create(person_from=b, person_to=d, kind=K.ACTIVITY_MATE, activity="Fútbol")

    relations = kinship.relations_for(b)

    assert [(r.person.display_name, str(r.label), r.tree, r.deduced) for r in relations] == [
        ("Ana", "Progenitor", "family", False),
        ("Carla", "Hermano", "family", True),
        ("Dani", "Compañero de actividad", "social", False),
    ]
    assert relations[2].activity == "Fútbol"
