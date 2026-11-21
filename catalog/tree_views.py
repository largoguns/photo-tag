"""«Árbol genealógico» screen (spec section 5.12): family tree and relationship network."""

from django.db.models import Count, Q
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from catalog import presenters
from catalog.models import Face, Person, RelationshipKind
from catalog.services import kinship, tree_layout

NAME_CHARS = 16  # longer names are cut in the drawing (the full one is in the title)


def _central_person(request):
    value = request.GET.get("person", "")
    if value.isdigit():
        person = Person.objects.filter(pk=value).first()
        if person:
            return person.resolved()
    return Person.objects.filter(user=request.user, merged_into__isnull=True).first()


def _people_to_choose():
    people = list(
        Person.objects.active()
        .filter(is_provisional=False)
        .annotate(
            photo_count=Count(
                "faces__photo", filter=Q(faces__status=Face.Status.ACTIVE), distinct=True
            )
        )
        .order_by("display_name")
    )
    avatars = presenters.person_avatars([p.pk for p in people])
    for person in people:
        person.avatar_id = avatars.get(person.pk)
    return people


def _flag(params, name):
    return params.get(name) != "0"


def _short(name):
    return name if len(name) <= NAME_CHARS else name[: NAME_CHARS - 1].rstrip() + "…"


def _level_title(mode, level):
    if mode == "social":
        return _("Relaciones directas") if level == 1 else _("Segundo grado")
    if level == 0:
        return _("Su generación")
    if level < 0:
        return ngettext("%(n)s generación por encima", "%(n)s generaciones por encima", -level) % {
            "n": -level
        }
    return ngettext("%(n)s generación por debajo", "%(n)s generaciones por debajo", level) % {
        "n": level
    }


def tree(request):
    params = request.GET
    mode = "social" if params.get("mode") == "social" else "family"
    focus = _central_person(request)
    context = {"params": params, "mode": mode, "focus": focus, "people": _people_to_choose()}
    if focus is None:
        return render(request, "catalog/tree.html", context)

    graph = kinship.Graph.load()
    if mode == "family":
        up = tree_layout.clamp_generations(params.get("up"))
        down = tree_layout.clamp_generations(params.get("down"))
        layout = tree_layout.family_tree(
            graph,
            focus.pk,
            up=up,
            down=down,
            partners=_flag(params, "partners"),
            in_laws=_flag(params, "in_laws"),
            direct=_flag(params, "direct"),
        )
        context.update(
            up=up,
            down=down,
            generation_choices=[(n, str(n)) for n in range(1, tree_layout.MAX_GENERATIONS + 1)],
            family_toggles=[
                ("partners", _("Parejas"), _flag(params, "partners")),
                ("in_laws", _("Familia política"), _flag(params, "in_laws")),
                ("direct", _("Enlaces directos"), _flag(params, "direct")),
            ],
        )
    else:
        kind = params.get("kind", "")
        kind = kind if kind in kinship.SOCIAL_KINDS else ""
        layout = tree_layout.social_graph(
            graph,
            focus.pk,
            kinds=[kind] if kind else None,
            activity=params.get("activity", ""),
            depth=params.get("depth", "1"),
        )
        activities = sorted(
            {act for *_rest, k, act in graph.edges if k == RelationshipKind.ACTIVITY_MATE and act}
        )
        context.update(
            kind=kind,
            kind_label=RelationshipKind(kind).label if kind else "",
            kind_options=[
                (k.value, k.label) for k in RelationshipKind if k in kinship.SOCIAL_KINDS
            ],
            activities=activities,
            activity_choices=[(a, a) for a in activities],
            legend=[(k, RelationshipKind(k).label) for k in layout.kinds],
        )

    persons = Person.objects.in_bulk([node.person_id for node in layout.nodes])
    avatars = presenters.person_avatars(list(persons))
    face_url = reverse("face_image", args=[0]).replace("/0/", "/{id}/")
    nodes = []
    for node in layout.nodes:
        person = persons.get(node.person_id)
        if person is None:
            continue
        label = str(kinship.role_label(node.role)) if node.role else ""
        if node.activity:
            label = f"{label} · {node.activity}"
        avatar = avatars.get(person.pk)
        nodes.append(
            {
                "node": node,
                "person": person,
                "short_name": _short(person.shown_name),
                "role": label,
                "avatar_url": face_url.replace("{id}", str(avatar)) if avatar else "",
                "image_x": node.x - tree_layout.NODE_R,
                "image_y": node.y - tree_layout.NODE_R,
                "name_y": node.y + tree_layout.NODE_R + 18,
                "role_y": node.y + tree_layout.NODE_R + 34,
            }
        )
    levels = {}
    for item in sorted(nodes, key=lambda i: (i["node"].level, i["node"].x)):
        if not item["node"].focus:
            levels.setdefault(item["node"].level, []).append(item)
    context.update(
        layout=layout,
        nodes=nodes,
        node_r=tree_layout.NODE_R,
        list_view=params.get("view") == "list",
        list_levels=[(_level_title(mode, level), items) for level, items in sorted(levels.items())],
    )
    return render(request, "catalog/tree.html", context)
