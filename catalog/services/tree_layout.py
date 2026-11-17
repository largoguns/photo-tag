"""Layout of the family tree and of the relationship network (spec section 5.12).

Pure functions over a `kinship.Graph`: they choose who appears around a central person
and give every node coordinates and every edge an SVG path. The template only draws.

Family tree: one row per generation (ancestors above, descendants below). A person and
their partners or ex-partners form a *unit* drawn side by side; children hang from the
middle of their parents' unit. Units are placed as a tidy forest from the top, then each
row is nudged so parents sit over their children where there is room.

Relationship network: the central person in the middle, direct relationships on a first
ring and, optionally, theirs on a second ring.
"""

import math
from collections import defaultdict
from dataclasses import dataclass, field

from catalog.models import RelationshipKind
from catalog.services import kinship
from catalog.services.names import name_key

K = RelationshipKind

SLOT = 128  # horizontal room for one person (node and name)
ROW = 168  # vertical distance between generations
NODE_R = 28  # avatar radius
LABEL = 30  # room for the name under the avatar
MARGIN = 72
MAX_FAMILY_NODES = 400
MAX_SOCIAL_NODES = 200
MAX_GENERATIONS = 5

# Generation of the other person relative to the central one, by role.
ROLE_OFFSET = {
    "parent": -1,
    "child": 1,
    "grandparent": -2,
    "grandchild": 2,
    "uncle": -1,
    "nephew": 1,
    "parent_in_law": -1,
    "child_in_law": 1,
    "sibling": 0,
    "cousin": 0,
    "sibling_in_law": 0,
    "partner": 0,
    "ex_partner": 0,
}
IN_LAW_KINDS = {K.PARENT_IN_LAW, K.SIBLING_IN_LAW}
DIRECT_KINDS = {K.GRANDPARENT, K.UNCLE, K.SIBLING, K.COUSIN}


@dataclass
class Node:
    person_id: int
    x: float
    y: float
    level: int  # generation (family) or ring (network)
    role: str = ""  # role relative to the central person (kinship.ROLES), "" if none
    activity: str = ""
    focus: bool = False


@dataclass
class Edge:
    d: str  # SVG path
    style: str  # partner, ex_partner, parent, direct, in_law or a social kind


@dataclass
class Layout:
    nodes: list = field(default_factory=list)
    edges: list = field(default_factory=list)
    width: float = 0
    height: float = 0
    truncated: bool = False
    kinds: list = field(default_factory=list)  # social kinds drawn (legend)

    def node(self, person_id):
        return next((n for n in self.nodes if n.person_id == person_id), None)


def _clamp(value, low, high, default):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def clamp_generations(value, default=3):
    return _clamp(value, 1, MAX_GENERATIONS, default)


def _explicit_roles(graph, focus):
    """{other_id: (role, kind, activity)} of the relationships stored for `focus`."""
    roles = {}
    for person_from, person_to, kind, activity in graph.edges:
        if focus not in (person_from, person_to):
            continue
        is_from = person_from == focus
        other = person_to if is_from else person_from
        roles.setdefault(other, (kinship.role_of_other(kind, is_from), kind, activity))
    return roles


# ---------------------------------------------------------------------------
# Family tree
# ---------------------------------------------------------------------------


def _select_family(graph, focus, up, down, partners, in_laws, direct):
    """{person_id: generation} of the people drawn, and whether the cap was hit."""
    gen = {focus: 0}
    truncated = False

    def add(pid, level):
        nonlocal truncated
        if pid in gen:
            return False
        if len(gen) >= MAX_FAMILY_NODES:
            truncated = True
            return False
        gen[pid] = level
        return True

    # Ancestors (and explicit grandparents when direct links are shown).
    queue = [focus]
    while queue:
        current = queue.pop(0)
        for parent in sorted(graph.parents[current]):
            if gen[current] - 1 >= -up and add(parent, gen[current] - 1):
                queue.append(parent)
    ancestors = [pid for pid, level in gen.items() if level < 0]

    # Descendants of the ancestors and of the central person: siblings, uncles, cousins…
    queue = [focus, *sorted(ancestors, key=lambda pid: (gen[pid], pid))]
    while queue:
        current = queue.pop(0)
        for child in sorted(graph.children[current]):
            if gen[current] + 1 <= down and add(child, gen[current] + 1):
                queue.append(child)

    if partners:
        for pid in list(gen):
            for other in sorted(graph.partners[pid] | graph.ex_partners[pid]):
                add(other, gen[pid])

    for other, (role, kind, _activity) in _explicit_roles(graph, focus).items():
        wanted = (in_laws and kind in IN_LAW_KINDS) or (direct and kind in DIRECT_KINDS)
        level = ROLE_OFFSET.get(role)
        if wanted and level is not None and -up <= level <= down:
            add(other, level)
    return gen, truncated


def _units(graph, gen, partners):
    """Units per generation: a person with their partners/ex-partners in the same row."""
    unit_of = {}
    units = []
    for pid in sorted(gen, key=lambda p: (gen[p], p)):
        if pid in unit_of:
            continue
        members, stack = [], [pid]
        while stack:
            current = stack.pop()
            if current in unit_of:
                continue
            unit_of[current] = len(units)
            members.append(current)
            if partners:
                for other in sorted(graph.partners[current] | graph.ex_partners[current]):
                    if other in gen and gen[other] == gen[current] and other not in unit_of:
                        stack.append(other)
        # Members with parents in the tree go first, so their line comes from the left.
        members.sort(key=lambda p: (not any(par in gen for par in graph.parents[p]), p))
        units.append(members)
    return units, unit_of


def family_tree(graph, focus, up=3, down=3, partners=True, in_laws=True, direct=True):
    up, down = clamp_generations(up), clamp_generations(down)
    gen, truncated = _select_family(graph, focus, up, down, partners, in_laws, direct)
    units, unit_of = _units(graph, gen, partners)
    level = [gen[members[0]] for members in units]

    def children_units(u):
        result = []
        for member in units[u]:
            for child in sorted(graph.children[member]):
                if child in gen and gen[child] == level[u] + 1:
                    cu = unit_of[child]
                    if cu not in result:
                        result.append(cu)
        return result

    def has_parents(u):
        return any(p in gen and gen[p] == level[u] - 1 for m in units[u] for p in graph.parents[m])

    center = {}  # unit → x of its middle
    claimed = set()

    def place(u, left):
        """Place unit `u` and its descendants from `left`; return the width used."""
        own = len(units[u]) * SLOT
        kids = [c for c in children_units(u) if c not in claimed]
        claimed.update(kids)
        cursor = left
        for kid in kids:
            cursor += place(kid, cursor)
        width = max(own, cursor - left)
        if kids:
            middle = (center[kids[0]] + center[kids[-1]]) / 2
            middle = min(max(middle, left + own / 2), left + width - own / 2)
        else:
            middle = left + width / 2
        center[u] = middle
        return width

    # Roots: the central person's ancestors' side first, then the rest, top to bottom.
    roots = [u for u in range(len(units)) if not has_parents(u)]
    roots.sort(key=lambda u: (level[u], min(units[u])))
    cursor = 0
    for root in roots:
        if root not in claimed:
            claimed.add(root)
            cursor += place(root, cursor)
    for u in range(len(units)):  # anything left (inconsistent data): to the right
        if u not in center:
            claimed.add(u)
            cursor += place(u, cursor)

    _nudge_parents(units, level, center, children_units)

    # Coordinates.
    top = min(level)
    x_of, y_of = {}, {}
    for u, members in enumerate(units):
        first = center[u] - len(members) * SLOT / 2 + SLOT / 2
        for i, pid in enumerate(members):
            x_of[pid] = first + i * SLOT
            y_of[pid] = (level[u] - top) * ROW
    min_x = min(x_of.values())
    for pid in x_of:
        x_of[pid] += MARGIN - min_x
        y_of[pid] += MARGIN

    explicit = _explicit_roles(graph, focus)
    deduced = graph.deduced(focus)
    layout = Layout(truncated=truncated)
    for pid in gen:
        role = explicit[pid][0] if pid in explicit else deduced.get(pid, "")
        layout.nodes.append(
            Node(pid, x_of[pid], y_of[pid], gen[pid], role=role, focus=pid == focus)
        )
    layout.edges = _family_edges(graph, gen, units, unit_of, x_of, y_of, focus, explicit)
    layout.width = max(x_of.values()) + MARGIN
    layout.height = max(y_of.values()) + NODE_R + LABEL + MARGIN
    return layout


def _nudge_parents(units, level, center, children_units):
    """Move each unit towards the middle of its children, keeping the row's order and
    never overlapping its neighbours. Bottom rows first."""
    rows = defaultdict(list)
    for u in range(len(units)):
        rows[level[u]].append(u)
    for row in sorted(rows, reverse=True):
        ordered = sorted(rows[row], key=lambda u: center[u])
        for index, u in enumerate(ordered):
            kids = children_units(u)
            if not kids:
                continue
            target = sum(center[k] for k in kids) / len(kids)
            half = len(units[u]) * SLOT / 2
            low, high = -math.inf, math.inf
            if index > 0:
                prev = ordered[index - 1]
                low = center[prev] + len(units[prev]) * SLOT / 2 + half
            if index + 1 < len(ordered):
                nxt = ordered[index + 1]
                high = center[nxt] - len(units[nxt]) * SLOT / 2 - half
            center[u] = min(max(target, low), high) if low <= high else center[u]


def _family_edges(graph, gen, units, unit_of, x_of, y_of, focus, explicit):
    edges = []
    # Partners side by side (a line behind any member in between).
    for members in units:
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                if b in graph.partners[a]:
                    style = "partner"
                elif b in graph.ex_partners[a]:
                    style = "ex_partner"
                else:
                    continue
                left, right = sorted((a, b), key=lambda p: x_of[p])
                edges.append(
                    Edge(f"M{x_of[left] + NODE_R},{y_of[a]} H{x_of[right] - NODE_R}", style)
                )
    # Children from their parents (from the middle of a couple when both are in it).
    for child in gen:
        parents = [p for p in graph.parents[child] if p in gen and gen[p] == gen[child] - 1]
        if not parents:
            continue
        cx, cy = x_of[child], y_of[child] - NODE_R
        groups = defaultdict(list)
        for parent in parents:
            groups[unit_of[parent]].append(parent)
        for members in groups.values():
            if len(members) == 2:
                sx = (x_of[members[0]] + x_of[members[1]]) / 2
                sy = y_of[members[0]]
            else:
                sx, sy = x_of[members[0]], y_of[members[0]] + NODE_R + LABEL
            mid = cy - (ROW - NODE_R * 2 - LABEL) / 2
            edges.append(Edge(f"M{sx},{sy} V{mid} H{cx} V{cy}", "parent"))
    # Explicit in-law and direct links of the central person.
    for other, (_role, kind, _activity) in explicit.items():
        if other not in gen or kind in (K.PARENT, K.PARTNER, K.EX_PARTNER):
            continue
        if kind not in kinship.FAMILY_KINDS:
            continue
        style = "in_law" if kind in IN_LAW_KINDS else "direct"
        edges.append(Edge(f"M{x_of[focus]},{y_of[focus]} L{x_of[other]},{y_of[other]}", style))
    return edges


# ---------------------------------------------------------------------------
# Relationship network
# ---------------------------------------------------------------------------


def social_graph(graph, focus, kinds=None, activity="", depth=1):
    """The central person and their social relationships, on one or two rings."""
    depth = 2 if str(depth) == "2" else 1
    kinds = {k for k in (kinds or []) if k in kinship.SOCIAL_KINDS} or set(kinship.SOCIAL_KINDS)
    wanted_activity = name_key(activity) if activity else ""

    adjacency = defaultdict(list)  # pid → [(other, kind, activity, is_from)]
    for person_from, person_to, kind, act in graph.edges:
        if kind not in kinds:
            continue
        if wanted_activity and (kind != K.ACTIVITY_MATE or name_key(act) != wanted_activity):
            continue
        adjacency[person_from].append((person_to, kind, act, True))
        adjacency[person_to].append((person_from, kind, act, False))

    layout = Layout()
    ring1, info = [], {}
    for other, kind, act, is_from in sorted(adjacency[focus], key=lambda e: (e[0], e[1])):
        if other not in info:
            ring1.append(other)
            info[other] = (kinship.role_of_other(kind, is_from), act)
    truncated = len(ring1) > MAX_SOCIAL_NODES
    ring1 = ring1[:MAX_SOCIAL_NODES]
    ring2 = []  # (pid, via)
    if depth == 2:
        seen = {focus, *ring1}
        for via in ring1:
            for other, _kind, _act, _is_from in sorted(adjacency[via], key=lambda e: e[0]):
                if other not in seen and len(ring1) + len(ring2) < MAX_SOCIAL_NODES:
                    seen.add(other)
                    ring2.append((other, via))
    layout.truncated = truncated

    r1 = max(SLOT * 1.4, len(ring1) * SLOT / (2 * math.pi))
    r2 = max(r1 + ROW, len(ring2) * SLOT / (2 * math.pi))
    pos = {focus: (0.0, 0.0)}
    for i, pid in enumerate(ring1):
        angle = -math.pi / 2 + 2 * math.pi * i / max(1, len(ring1))
        pos[pid] = (r1 * math.cos(angle), r1 * math.sin(angle))
    for i, (pid, _via) in enumerate(ring2):
        angle = -math.pi / 2 + 2 * math.pi * (i + 0.5) / max(1, len(ring2))
        pos[pid] = (r2 * math.cos(angle), r2 * math.sin(angle))

    radius = r2 if ring2 else (r1 if ring1 else 0)
    shift = radius + MARGIN + SLOT / 2
    for pid, (x, y) in pos.items():
        level = 0 if pid == focus else (1 if pid in info else 2)
        role, act = info.get(pid, ("", ""))
        layout.nodes.append(
            Node(pid, x + shift, y + shift, level, role=role, activity=act, focus=pid == focus)
        )
    drawn = set(pos)
    kinds_drawn = []
    for person_from, person_to, kind, act in graph.edges:
        if person_from not in drawn or person_to not in drawn:
            continue
        if kind not in kinds or (wanted_activity and name_key(act) != wanted_activity):
            continue
        (x1, y1), (x2, y2) = pos[person_from], pos[person_to]
        layout.edges.append(
            Edge(f"M{x1 + shift},{y1 + shift} L{x2 + shift},{y2 + shift}", str(kind))
        )
        if kind not in kinds_drawn:
            kinds_drawn.append(kind)
    layout.kinds = kinds_drawn
    layout.width = layout.height = 2 * shift
    return layout
