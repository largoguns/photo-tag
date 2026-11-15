"""Levels: family-themed names reached by points."""

from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _

LEVELS = [
    (0, _("Curioso")),
    (50, _("Aprendiz de archivero")),
    (150, _("Archivero")),
    (400, _("Detective de familia")),
    (900, _("Cronista")),
    (1800, _("Memoria de la familia")),
    (3500, _("Guardián del álbum")),
]


@dataclass
class Level:
    number: int  # 1-based
    name: str
    points: int
    next_name: str | None
    next_at: int | None

    @property
    def missing(self):
        return max(0, self.next_at - self.points) if self.next_at is not None else 0

    @property
    def percent(self):
        if self.next_at is None:
            return 100
        start = LEVELS[self.number - 1][0]
        return round(100 * (self.points - start) / (self.next_at - start))


def level_for(points):
    index = max(i for i, (threshold, _name) in enumerate(LEVELS) if points >= threshold)
    following = LEVELS[index + 1] if index + 1 < len(LEVELS) else (None, None)
    return Level(
        number=index + 1,
        name=LEVELS[index][1],
        points=points,
        next_name=following[1],
        next_at=following[0],
    )
