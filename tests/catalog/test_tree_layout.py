import time
from collections import defaultdict

from catalog.models import Relationship
from catalog.services import tree_layout
from catalog.services.kinship import Graph
from catalog.services.tree_layout import NODE_R, SLOT

K = Relationship.Kind

# Ids:
#   10 ─┬─ 11          12 ─┬─ 13           great-grandparent 20 → 10
#       └── 1 ─┬─ 2 ───────┘ (2: child of 12 and 13)      14: uncle (12, 13 → 14)
#              ├── 3 (focus) ─┬─ 30 (partner)    31 (ex-partner of 3)
#              │              ├── 40 (child of 3 and 30)
#              │              └── 41 (child of 3 and 31: half sibling of 40)
#              └── 4 (sibling) → 42 (nephew)
#   15: child of 14 (cousin)       50: friend of 3, 51: coworker of 50
ROWS = [
    (20, 10, K.PARENT),
    (10, 11, K.PARTNER),
    (10, 1, K.PARENT),
    (11, 1, K.PARENT),
    (12, 13, K.PARTNER),
    (12, 2, K.PARENT),
    (13, 2, K.PARENT),
    (12, 14, K.PARENT),
    (13, 14, K.PARENT),
    (14, 15, K.PARENT),
    (1, 2, K.PARTNER),
    (1, 3, K.PARENT),
    (2, 3, K.PARENT),
    (1, 4, K.PARENT),
    (2, 4, K.PARENT),
    (3, 30, K.PARTNER),
    (3, 31, K.EX_PARTNER),
    (3, 40, K.PARENT),
    (30, 40, K.PARENT),
    (3, 41, K.PARENT),
    (31, 41, K.PARENT),
    (4, 42, K.PARENT),
    (3, 50, K.FRIEND),
    (50, 51, K.COWORKER),
]


def graph(rows=ROWS):
    return Graph(rows)


def positions(layout):
    return {node.person_id: (node.x, node.y) for node in layout.nodes}


def assert_no_overlaps(layout):
    rows = defaultdict(list)
    for node in layout.nodes:
        rows[node.y].append(node.x)
    for xs in rows.values():
        xs.sort()
        assert all(b - a >= SLOT - 0.01 for a, b in zip(xs, xs[1:], strict=False)), xs


def test_generations_go_in_rows():
    layout = tree_layout.family_tree(graph(), 3)
    pos = positions(layout)
    levels = {node.person_id: node.level for node in layout.nodes}

    assert levels[3] == 0 and levels[1] == levels[2] == -1 and levels[10] == -2
    assert levels[20] == -3 and levels[40] == levels[42] == 1
    assert pos[1][1] < pos[3][1] < pos[40][1]
    assert pos[3][1] == pos[4][1] == pos[15][1]  # siblings and cousins on the same row
    assert layout.node(3).focus


def test_nobody_overlaps_and_partners_are_adjacent():
    layout = tree_layout.family_tree(graph(), 3)
    pos = positions(layout)
    assert_no_overlaps(layout)
    assert abs(pos[1][0] - pos[2][0]) == SLOT
    assert {abs(pos[3][0] - pos[30][0]), abs(pos[3][0] - pos[31][0])} <= {SLOT, 2 * SLOT}


def test_children_hang_from_their_parents():
    layout = tree_layout.family_tree(graph(), 3)
    pos = positions(layout)
    left, right = sorted((pos[1][0], pos[2][0]))
    # The couple sits over its children.
    kids = (pos[3][0], pos[4][0])
    assert min(kids) - SLOT <= (left + right) / 2 <= max(kids) + SLOT
    parent_edges = [e for e in layout.edges if e.style == "parent"]
    middle = (pos[1][0] + pos[2][0]) / 2
    assert any(e.d.startswith(f"M{middle},{pos[1][1]}") for e in parent_edges)
    # Half siblings: one connector from each couple.
    assert any(f"H{pos[41][0]} V{pos[41][1] - NODE_R}" in e.d for e in parent_edges)
    assert any(f"H{pos[40][0]} V{pos[40][1] - NODE_R}" in e.d for e in parent_edges)


def test_partner_and_ex_partner_edges():
    styles = {e.style for e in tree_layout.family_tree(graph(), 3).edges}
    assert {"partner", "ex_partner", "parent"} <= styles


def test_generation_limits():
    layout = tree_layout.family_tree(graph(), 3, up=1, down=1)
    ids = {n.person_id for n in layout.nodes}
    assert {1, 2, 4, 40, 41, 42} <= ids
    assert not ids & {10, 11, 12, 13, 14, 15, 20}
    assert tree_layout.clamp_generations("9") == 5
    assert tree_layout.clamp_generations("x") == 3


def test_partners_filter():
    ids = {n.person_id for n in tree_layout.family_tree(graph(), 3, partners=False).nodes}
    assert 30 not in ids and 31 not in ids
    assert 40 in ids  # children still appear


def test_in_laws_and_direct_links_filters():
    rows = [*ROWS, (60, 3, K.PARENT_IN_LAW), (3, 61, K.COUSIN)]
    full = tree_layout.family_tree(graph(rows), 3)
    assert {60, 61} <= {n.person_id for n in full.nodes}
    assert full.node(60).level == -1 and full.node(61).level == 0
    # Nothing joins them yet: no line, they show their kinship under the name instead.
    assert full.node(60).unlinked and full.node(61).unlinked
    assert {e.style for e in full.edges} <= {"partner", "ex_partner", "parent"}

    bare = tree_layout.family_tree(graph(rows), 3, in_laws=False, direct=False)
    assert not {60, 61} & {n.person_id for n in bare.nodes}


def test_roles_relative_to_the_center():
    layout = tree_layout.family_tree(graph(), 3)
    roles = {n.person_id: n.role for n in layout.nodes}
    assert roles[1] == "parent" and roles[4] == "sibling" and roles[15] == "cousin"
    assert roles[30] == "partner" and roles[10] == "grandparent" and roles[3] == ""


def test_a_person_without_relationships():
    layout = tree_layout.family_tree(graph(), 999)
    assert [n.person_id for n in layout.nodes] == [999]
    assert layout.width > 0 and layout.height > 0


def test_node_cap(monkeypatch):
    monkeypatch.setattr(tree_layout, "MAX_FAMILY_NODES", 5)
    layout = tree_layout.family_tree(graph(), 3)
    assert len(layout.nodes) == 5 and layout.truncated


def test_social_rings_depth_and_filters():
    rows = [*ROWS, (3, 52, K.ACTIVITY_MATE, "Fútbol"), (3, 53, K.ACTIVITY_MATE, "Coro")]
    g = graph([(*r, "") if len(r) == 3 else r for r in rows])

    one = tree_layout.social_graph(g, 3)
    assert {n.person_id for n in one.nodes} == {3, 50, 52, 53}  # family is not here
    assert one.node(50).role == "friend" and one.node(52).activity == "Fútbol"

    two = tree_layout.social_graph(g, 3, depth=2)
    assert two.node(51).level == 2
    assert {K.FRIEND, K.COWORKER} <= set(two.kinds)

    friends = tree_layout.social_graph(g, 3, kinds=["friend"])
    assert {n.person_id for n in friends.nodes} == {3, 50}
    football = tree_layout.social_graph(g, 3, activity="futbol")
    assert {n.person_id for n in football.nodes} == {3, 52}


def test_social_nodes_are_on_rings_around_the_center():
    layout = tree_layout.social_graph(graph(), 3)
    center = layout.node(3)
    friend = layout.node(50)
    assert friend.y < center.y  # the first one goes on top
    assert layout.width == layout.height


def test_large_family_is_fast():
    rows = []
    # 1 000 people: 10 generations of couples with three children each, wide but shallow.
    next_id = 2
    level = [1]
    while next_id < 1000:
        new_level = []
        for parent in level:
            partner = next_id
            rows.append((min(parent, partner), max(parent, partner), K.PARTNER))
            next_id += 1
            for _ in range(3):
                rows += [(parent, next_id, K.PARENT), (partner, next_id, K.PARENT)]
                new_level.append(next_id)
                next_id += 1
        level = new_level
    start = time.perf_counter()
    layout = tree_layout.family_tree(Graph(rows), level[0], up=5, down=5)
    assert time.perf_counter() - start < 0.3
    assert_no_overlaps(layout)


def _bars(layout):
    """Horizontal parent lines as (y, start x of the family, left, right)."""
    bars = set()
    for edge in layout.edges:
        if edge.style != "parent":
            continue
        start, rest = edge.d[1:].split(" V", 1)
        sx = float(start.split(",")[0])
        mid, rest = rest.split(" H", 1)
        cx = float(rest.split(" V")[0])
        bars.add((float(mid), sx, min(sx, cx), max(sx, cx)))
    return bars


def test_each_partner_sits_on_their_parents_side_and_family_lines_do_not_merge():
    # Two grandmothers with no relation between them: 60 → mum (63); 61 → dad (62) and his
    # sister 64 (partner 65). Dad and mum have 66 (focus) and 67.
    rows = [
        (60, 63, K.PARENT),
        (61, 62, K.PARENT),
        (61, 64, K.PARENT),
        (64, 65, K.PARTNER),
        (62, 63, K.PARTNER),
        (62, 66, K.PARENT),
        (63, 66, K.PARENT),
        (62, 67, K.PARENT),
        (63, 67, K.PARENT),
    ]
    layout = tree_layout.family_tree(Graph(rows), 66)
    pos = positions(layout)
    assert_no_overlaps(layout)
    dad, mum, aunt = pos[62][0], pos[63][0], pos[64][0]
    assert not min(dad, aunt) < mum < max(dad, aunt)  # mum is not between dad and his sister
    # Lines of different families never overlap at the same height.
    bars = _bars(layout)
    for y1, s1, l1, r1 in bars:
        for y2, s2, l2, r2 in bars:
            if y1 == y2 and s1 != s2:
                assert r1 < l2 or r2 < l1, (y1, (l1, r1), (l2, r2))


def _family_of_the_screenshots():
    """Grandmother 60 → dad 62 and aunt 64 (ex-partner 65, daughter 73); grandmother 61 →
    mum 63. Dad and mum → 66 (focus), 67, 68. 77: a cousin whose parent is not in the tree.
    The focus has explicit links to a sibling, the cousins and a sibling-in-law."""
    rows = [
        (60, 62, K.PARENT),
        (60, 64, K.PARENT),
        (61, 63, K.PARENT),
        (62, 63, K.PARTNER),
        (64, 65, K.EX_PARTNER),
        (64, 73, K.PARENT),
        (65, 73, K.PARENT),
        (68, 69, K.PARTNER),
        (66, 68, K.SIBLING),
        (66, 73, K.COUSIN),
        (66, 77, K.COUSIN),
        (66, 69, K.SIBLING_IN_LAW),
    ]
    for child in (66, 67, 68):
        rows += [(62, child, K.PARENT), (63, child, K.PARENT)]
    return tree_layout.family_tree(Graph(rows), 66)


def test_a_child_marrying_into_another_family_sits_on_its_edge():
    layout = _family_of_the_screenshots()
    pos = positions(layout)
    assert_no_overlaps(layout)
    grandma_dad, grandma_mum = pos[60][0], pos[61][0]
    dad, mum, aunt = pos[62][0], pos[63][0], pos[64][0]
    assert grandma_dad < grandma_mum and aunt < dad < mum
    # No family line crosses another one.
    bars = _bars(layout)
    for y1, s1, l1, r1 in bars:
        for y2, s2, l2, r2 in bars:
            if y1 == y2 and s1 != s2:
                assert r1 < l2 or r2 < l1


def test_only_relatives_the_tree_cannot_join_are_marked():
    layout = _family_of_the_screenshots()
    unlinked = {n.person_id for n in layout.nodes if n.unlinked}
    assert unlinked == {77}  # siblings, the cousin 73 and the sibling-in-law are joined
    assert layout.node(77).role == "cousin"


def test_unlinked_people_sit_next_to_their_row():
    layout = _family_of_the_screenshots()
    row = sorted(n.x for n in layout.nodes if n.y == layout.node(77).y)
    gaps = [b - a for a, b in zip(row, row[1:], strict=False)]
    assert layout.node(77).x == row[-1] and gaps[-1] == SLOT
