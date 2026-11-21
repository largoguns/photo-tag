import pytest
from django.urls import reverse
from django.utils import translation

from catalog.models import Person
from catalog.services import relationships

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def spanish():
    with translation.override("es"):
        yield


@pytest.fixture
def family(alice):
    names = ["Carmen", "José", "Paco", "Lola", "Luis", "Manolo"]
    people = {name: Person.objects.create(display_name=name) for name in names}
    relationships.add(alice, people["Carmen"], "partner", other=people["José"])
    relationships.add(alice, people["Paco"], "parent", other=people["Carmen"])
    relationships.add(alice, people["Paco"], "parent", other=people["José"])
    relationships.add(alice, people["Lola"], "parent", other=people["Carmen"])
    relationships.add(alice, people["Paco"], "child", other=people["Luis"])
    relationships.add(alice, people["Paco"], "friend", other=people["Manolo"])
    return people


def get(client, **params):
    return client.get(reverse("tree"), params).content.decode()


def test_login_is_required(client):
    response = client.get(reverse("tree"))
    assert response.status_code == 302 and "login" in response.url


def test_without_a_linked_person_it_asks_whom(client, alice, family):
    client.force_login(alice)
    content = get(client)
    assert "¿De quién quieres ver el árbol?" in content
    assert f"?person={family['Paco'].pk}" in content


def test_starts_with_the_linked_person(client, alice, family):
    paco = family["Paco"]
    paco.user = alice
    paco.save()
    client.force_login(alice)

    content = get(client)

    assert "data-pan-zoom" in content and "data-node-focus" in content
    for name in ("Carmen", "José", "Lola", "Luis"):
        assert f'data-node-name="{name}"' in content
    assert 'data-node-name="Manolo"' not in content  # friends are in the other tree
    assert 'data-node-role="Progenitor"' in content and 'data-node-role="Hermano"' in content
    assert "edge-partner" in content and "edge-parent" in content


def test_a_merged_person_is_resolved(client, alice, family):
    old = Person.objects.create(display_name="Paco viejo", merged_into=family["Paco"])
    client.force_login(alice)
    content = get(client, person=old.pk)
    focus = content.split("data-node-focus")[0].rsplit("<a ", 1)[1]
    assert 'data-node-name="Paco"' in focus


def test_filters_and_generations(client, alice, family):
    client.force_login(alice)
    content = get(client, person=family["Luis"].pk, up=1)
    assert 'data-node-name="Paco"' in content
    assert 'data-node-name="Carmen"' not in content
    assert "1 generación arriba" in content


def test_social_mode(client, alice, family):
    client.force_login(alice)
    content = get(client, person=family["Paco"].pk, mode="social")
    assert 'data-node-name="Manolo"' in content and 'data-node-role="Amigo"' in content
    assert 'data-node-name="Carmen"' not in content
    assert "tree-legend" in content and "edge-friend" in content


def test_list_view(client, alice, family):
    client.force_login(alice)
    content = get(client, person=family["Paco"].pk, view="list")
    assert "tree-list" in content and "data-pan-zoom" not in content
    assert "1 generación por encima" in content and "Su generación" in content
    assert "Carmen" in content


def test_nodes_center_the_tree_and_open_the_person(client, alice, family):
    client.force_login(alice)
    content = get(client, person=family["Paco"].pk)
    carmen = family["Carmen"]
    assert f'href="{reverse("tree")}?person={carmen.pk}"' in content
    assert f'data-node-add="{reverse("person_detail", args=[carmen.pk])}?sheet=family"' in content


def test_navigation_and_person_page_link_to_the_tree(client, alice, family):
    client.force_login(alice)
    content = client.get(reverse("person_detail", args=[family["Paco"].pk])).content.decode()
    assert f"{reverse('tree')}?person={family['Paco'].pk}" in content
    assert 'class="nav-item" href="/tree/"' in content.replace(reverse("tree"), "/tree/")


def test_lonely_person(client, alice):
    alone = Person.objects.create(display_name="Sola")
    client.force_login(alice)
    content = get(client, person=alone.pk)
    assert "Todavía no hay familia de Sola" in content


def test_a_relative_without_the_people_in_between_shows_the_kinship(client, alice, family):
    paco = family["Paco"]
    susi = Person.objects.create(display_name="Susi")
    relationships.add(alice, paco, "cousin", other=susi)
    client.force_login(alice)

    content = get(client, person=paco.pk)

    assert 'class="node-role"' in content and ">primo</text>" in content
    assert content.count('class="node-role"') == 1  # Lola is joined through their parents
