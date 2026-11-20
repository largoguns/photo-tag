"""Kinship rules (spec section 5.12): roles, labels, canonical form and deductions.

A relationship is stored once in canonical form (see `RelationshipKind`):

- asymmetric kinds go downwards, `person_from` being the upper role: parent→child,
  grandparent→grandchild, uncle→nephew, parent-in-law→child-in-law, godparent→godchild;
- symmetric kinds go with `person_from.id < person_to.id`.

The interface speaks in *roles*: "<other> is <role> of <subject>". This module translates
between both and deduces family relationships from the primary ones (parent and partner).
Nothing here writes to the database.
"""

from collections import defaultdict
from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _

from catalog.models import ASYMMETRIC_KINDS, Person, Relationship, RelationshipKind

K = RelationshipKind

FAMILY, SOCIAL = "family", "social"

FAMILY_KINDS = frozenset(
    {
        K.PARENT,
        K.GRANDPARENT,
        K.UNCLE,
        K.PARENT_IN_LAW,
        K.SIBLING,
        K.COUSIN,
        K.SIBLING_IN_LAW,
        K.PARTNER,
        K.EX_PARTNER,
    }
)
SOCIAL_KINDS = frozenset(set(K) - FAMILY_KINDS)
# Kinds that make `person_from` an ancestor of `person_to`.
ANCESTRY_KINDS = frozenset({K.PARENT, K.GRANDPARENT})

# Asymmetric kind → (upper role, lower role).
_ASYMMETRIC_ROLES = {
    K.PARENT: ("parent", "child"),
    K.GRANDPARENT: ("grandparent", "grandchild"),
    K.UNCLE: ("uncle", "nephew"),
    K.PARENT_IN_LAW: ("parent_in_law", "child_in_law"),
    K.GODPARENT: ("godparent", "godchild"),
}

# role → (kind, True if the role is the `person_from` side).
ROLES = {}
for _kind, (_upper, _lower) in _ASYMMETRIC_ROLES.items():
    ROLES[_upper] = (_kind, True)
    ROLES[_lower] = (_kind, False)
for _kind in K:
    if _kind not in ASYMMETRIC_KINDS:
        ROLES[_kind.value] = (_kind, None)

ROLE_LABELS = {
    "parent": _("Progenitor"),
    "child": _("Hijo"),
    "grandparent": _("Abuelo"),
    "grandchild": _("Nieto"),
    "uncle": _("Tío"),
    "nephew": _("Sobrino"),
    "parent_in_law": _("Suegro"),
    "child_in_law": _("Hijo político"),
    "godparent": _("Padrino"),
    "godchild": _("Ahijado"),
    **{kind.value: kind.label for kind in K if kind not in ASYMMETRIC_KINDS},
}

# Roles offered in the interface, family first, in a natural reading order.
FAMILY_ROLES = [
    "parent",
    "child",
    "sibling",
    "partner",
    "ex_partner",
    "grandparent",
    "grandchild",
    "uncle",
    "nephew",
    "cousin",
    "parent_in_law",
    "child_in_law",
    "sibling_in_law",
]
SOCIAL_ROLES = [
    "friend",
    "compadre",
    "godparent",
    "godchild",
    "neighbor",
    "coworker",
    "classmate",
    "activity_mate",
]


def tree_of(kind):
    return FAMILY if kind in FAMILY_KINDS else SOCIAL


def canonical(subject_id, role, other_id):
    """'<other> is <role> of <subject>' → (person_from_id, person_to_id, kind)."""
    try:
        kind, other_is_from = ROLES[role]
    except KeyError:
        raise ValueError(f"unknown role {role!r}") from None
    if other_is_from is None:
        return min(subject_id, other_id), max(subject_id, other_id), kind
    if other_is_from:
        return other_id, subject_id, kind
    return subject_id, other_id, kind


def role_of_other(kind, viewer_is_from):
    """Role of the other person of a relationship, seen from the viewer."""
    if kind not in ASYMMETRIC_KINDS:
        return K(kind).value
    upper, lower = _ASYMMETRIC_ROLES[K(kind)]
    # The viewer is the upper side, so the other one is the lower side, and vice versa.
    return lower if viewer_is_from else upper


def role_label(role):
    return ROLE_LABELS[role]


# ---------------------------------------------------------------------------
# Graph of accepted relationships
# ---------------------------------------------------------------------------


class Graph:
    """Relationships in memory, loaded with a single query.

    `rows` are (person_from, person_to, kind) or (person_from, person_to, kind, activity).
    """

    def __init__(self, rows=()):
        self.edges = []  # (person_from, person_to, kind, activity), every relationship
        self.parents = defaultdict(set)
        self.children = defaultdict(set)
        self.partners = defaultdict(set)
        self.ex_partners = defaultdict(set)
        self.siblings = defaultdict(set)  # explicit sibling relationships only
        self.cousins = defaultdict(set)  # explicit cousin relationships only
        self.uncles = defaultdict(set)  # explicit: person → their uncles
        self._closure = None
        self.upper = defaultdict(set)  # ancestors one explicit step away (parent/grandparent)
        self.family = {}  # frozenset(pair) → kind, the explicit family relationship of a pair
        for row in rows:
            person_from, person_to, kind = row[:3]
            self.edges.append((person_from, person_to, kind, row[3] if len(row) > 3 else ""))
            if kind in FAMILY_KINDS:
                self.family[frozenset((person_from, person_to))] = kind
            if kind == K.PARENT:
                self.parents[person_to].add(person_from)
                self.children[person_from].add(person_to)
            elif kind == K.PARTNER:
                self.partners[person_from].add(person_to)
                self.partners[person_to].add(person_from)
            elif kind == K.EX_PARTNER:
                self.ex_partners[person_from].add(person_to)
                self.ex_partners[person_to].add(person_from)
            elif kind == K.SIBLING:
                self.siblings[person_from].add(person_to)
                self.siblings[person_to].add(person_from)
            elif kind == K.COUSIN:
                self.cousins[person_from].add(person_to)
                self.cousins[person_to].add(person_from)
            elif kind == K.UNCLE:
                self.uncles[person_to].add(person_from)
            if kind in ANCESTRY_KINDS:
                self.upper[person_to].add(person_from)

    @classmethod
    def load(cls, exclude_family_pair=None):
        """All accepted relationships, optionally ignoring the family one of a pair (used
        when a proposal would replace it)."""
        skip = frozenset(exclude_family_pair) if exclude_family_pair else None
        rows = Relationship.objects.values_list(
            "person_from_id", "person_to_id", "kind", "activity"
        )
        return cls(
            row
            for row in rows
            if skip is None or row[2] not in FAMILY_KINDS or frozenset(row[:2]) != skip
        )

    def ancestors(self, pid):
        """Everyone above `pid` through parent and grandparent relationships."""
        seen, stack = set(), list(self.upper[pid])
        while stack:
            current = stack.pop()
            if current not in seen:
                seen.add(current)
                stack.extend(self.upper[current])
        return seen

    def _sibling_component(self, pid):
        """`pid` and everyone linked to them by explicit sibling relationships (transitive)."""
        seen, stack = {pid}, [pid]
        while stack:
            for other in self.siblings[stack.pop()]:
                if other not in seen:
                    seen.add(other)
                    stack.append(other)
        return seen

    def siblings_of(self, pid):
        """Explicit siblings (transitively) and the children of their parents.

        Shared parents are not chained: a half-sibling's half-sibling is no sibling.
        """
        component = self._sibling_component(pid)
        shared = {c for member in component for p in self.parents[member] for c in self.children[p]}
        return (component | shared) - {pid}

    def _uncles_and_cousins(self):
        """{person: uncles}, {person: cousins} closed under the family rules, computed once.

        - my parents' siblings and my siblings' explicit uncles are my uncles;
        - my uncles' children and my siblings' explicit cousins are my cousins;
        - my cousins' siblings are my cousins, and I am theirs (with my siblings);
        - my cousins' parents are my uncles.
        Nothing further: no second cousins and no great-uncles.
        """
        if self._closure is not None:
            return self._closure
        people = set(self.parents) | set(self.children) | set(self.siblings)
        people |= set(self.cousins) | set(self.uncles)
        people |= {u for us in self.uncles.values() for u in us}
        sibs = {pid: self.siblings_of(pid) for pid in people}
        uncles = {pid: set() for pid in people}
        cousins = {pid: set() for pid in people}
        for pid in people:
            for p in self.parents[pid]:
                uncles[pid] |= sibs.get(p) or self.siblings_of(p)
            for s in sibs[pid] | {pid}:
                uncles[pid] |= self.uncles[s]
                cousins[pid] |= self.cousins[s]
        changed = True
        while changed:
            before = sum(map(len, uncles.values())) + sum(map(len, cousins.values()))
            for pid in people:
                for s in sibs[pid]:  # siblings share uncles and cousins
                    uncles[pid] |= uncles.get(s, set())
                    cousins[pid] |= cousins.get(s, set())
                for u in list(uncles[pid]):
                    cousins[pid] |= self.children[u]
                for c in list(cousins[pid]):
                    cousins[pid] |= sibs.get(c) or self.siblings_of(c)
                    uncles[pid] |= self.parents[c]
                    cousins.setdefault(c, set()).update({pid} | sibs[pid])
            for pid in people:
                excluded = {pid} | sibs[pid] | self.parents[pid] | self.children[pid]
                uncles[pid] -= excluded
                cousins[pid] -= excluded | uncles[pid]
            after = sum(map(len, uncles.values())) + sum(map(len, cousins.values()))
            changed = after != before
        nephews = defaultdict(set)
        for pid, ups in uncles.items():
            for u in ups:
                nephews[u].add(pid)
        self._closure = (uncles, cousins, nephews)
        return self._closure

    def deduced(self, pid):
        """{other_id: role} of the family relationships that follow from the primary ones.

        Pairs with an explicit family relationship are left out (they are not deduced).
        """
        parents = self.parents[pid]
        children = self.children[pid]
        partners = self.partners[pid]
        siblings = self.siblings_of(pid)
        all_uncles, all_cousins, all_nephews = self._uncles_and_cousins()
        groups = [
            ("sibling", siblings),
            ("grandparent", {g for p in parents for g in self.parents[p]}),
            ("grandchild", {g for c in children for g in self.children[c]}),
            ("uncle", all_uncles.get(pid, set())),
            ("nephew", all_nephews.get(pid, set())),
            ("cousin", all_cousins.get(pid, set())),
            ("parent_in_law", {p for q in partners for p in self.parents[q]}),
            ("child_in_law", {q for c in children for q in self.partners[c]}),
            (
                "sibling_in_law",
                {s for q in partners for s in self.siblings_of(q)}
                | {q for s in siblings for q in self.partners[s]},
            ),
        ]
        result = {}
        for role, others in groups:  # closest relationship first
            for other in others:
                if other == pid or other in result:
                    continue
                if frozenset((pid, other)) in self.family:
                    continue
                result[other] = role
        return result


# ---------------------------------------------------------------------------
# Relationships of one person, for the interface
# ---------------------------------------------------------------------------


@dataclass
class Relation:
    person: Person
    role: str
    tree: str
    deduced: bool = False
    activity: str = ""
    relationship: Relationship | None = None

    @property
    def label(self):
        return role_label(self.role)


def relations_for(person, graph=None):
    """Explicit and deduced relationships of `person`, family first."""
    pid = person.pk
    explicit = list(
        Relationship.objects.filter(person_from=pid).select_related("person_to")
    ) + list(Relationship.objects.filter(person_to=pid).select_related("person_from"))
    relations = []
    for rel in explicit:
        is_from = rel.person_from_id == pid
        other = rel.person_to if is_from else rel.person_from
        relations.append(
            Relation(
                person=other,
                role=role_of_other(rel.kind, is_from),
                tree=tree_of(rel.kind),
                activity=rel.activity,
                relationship=rel,
            )
        )
    graph = graph or Graph.load()
    deduced = graph.deduced(pid)
    others = Person.objects.in_bulk(deduced)
    for other_id, role in deduced.items():
        if other_id in others:
            relations.append(
                Relation(person=others[other_id], role=role, tree=FAMILY, deduced=True)
            )
    order = {role: i for i, role in enumerate(FAMILY_ROLES + SOCIAL_ROLES)}
    relations.sort(key=lambda r: (r.tree != FAMILY, r.deduced, order[r.role], r.person.shown_name))
    return relations
