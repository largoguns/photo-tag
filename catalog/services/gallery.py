"""Gallery in blocks: a light skeleton of every block up front, photos loaded per block.

Like Google Photos and Immich: the page gets the list of blocks (section, count and the sum
of aspect ratios, enough to estimate each block's height), and the browser loads a block
when it gets close and frees it when it goes far, so memory stays flat with any number
of photos.
"""

from dataclasses import dataclass, field

from django.db.models import Case, Count, F, IntegerField, Q, Value, When
from django.utils.translation import gettext

from catalog.models import Face, Location, Person, Photo, Question
from catalog.services import voting

BLOCK_SIZE = 120
QUESTIONS_STATUS = "questions"  # «Estado» option: photos with open questions (5.17)


def selected_people(params):
    """Active people chosen in the filter (merged ones resolved), without duplicates."""
    ids = [int(v) for v in params.getlist("person") if str(v).isdigit()]
    people = []
    for person in Person.objects.filter(pk__in=ids):
        person = person.resolved()
        if person not in people:
            people.append(person)
    return people


def selected_location(params):
    """The location chosen in the filter (a merged one resolved to its target), or None."""
    value = params.get("location", "")
    location = Location.objects.filter(pk=value).first() if value.isdigit() else None
    return location.resolved() if location else None


def location_options():
    """Locations with visible photos, with how many each one has."""
    return list(
        Location.objects.filter(merged_into__isnull=True, is_provisional=False)
        .annotate(photo_count=Count("photos", filter=Q(photos__discarded=False)))
        .filter(photo_count__gt=0)
        .order_by("name")
    )


def gallery_queryset(params, user):
    """Visible photos, newest year first (decade-only after precise dates, undated last)."""
    photos = (
        Photo.objects.visible()
        .annotate(
            decade_only=Case(
                When(date_precision=Photo.DatePrecision.DECADE, then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            )
        )
        .order_by(F("date_value").desc(nulls_last=True), "decade_only", "-pk")
    )
    if params.get("status") in Photo.Status.values:
        photos = photos.filter(status=params["status"])
    elif params.get("status") == QUESTIONS_STATUS:
        photos = photos.filter(questions__status=Question.Status.OPEN).distinct()
    people = selected_people(params)
    if people:
        # Every chosen person appears in the photo («salen todas juntas»).
        ids = [person.pk for person in people]
        photos = photos.annotate(
            chosen=Count(
                "faces__person",
                distinct=True,
                filter=Q(faces__person__in=ids, faces__status=Face.Status.ACTIVE),
            )
        ).filter(chosen=len(ids))
    if params.get("event", "").isdigit():
        photos = photos.filter(event_type_id=params["event"])
    if params.get("location", "").isdigit():
        location = selected_location(params)
        photos = photos.filter(location=location) if location else photos.none()
    if params.get("decade", "").isdigit():
        decade = int(params["decade"])
        photos = photos.filter(date_value__year__gte=decade, date_value__year__lt=decade + 10)
    if params.get("mine"):
        photos = photos.filter(pk__in=voting.photos_pending_for_user(user).values("pk"))
    return photos


def _section(date_value, precision):
    if not date_value:
        return "none", gettext("Sin fecha")
    if precision == Photo.DatePrecision.DECADE:
        decade = date_value.year // 10 * 10
        return f"d{decade}", gettext("Década de %(decade)s") % {"decade": decade}
    return f"y{date_value.year}", str(date_value.year)


@dataclass
class Block:
    index: int
    key: str  # section: "y1985", "d1980" or "none"
    label: str
    header: bool  # first block of its section: shows the section title
    ids: list = field(default_factory=list)
    ratio: float = 0.0  # sum of width/height: estimates the block height before loading
    photos: list = None  # filled only for the blocks sent with the page

    @property
    def count(self):
        return len(self.ids)


def blocks(queryset, size=None):
    """Every block of the gallery, in order, from a light query (no photo objects)."""
    size = size or BLOCK_SIZE
    result = []
    rows = queryset.values_list("pk", "width", "height", "date_value", "date_precision")
    for pk, width, height, date_value, precision in rows:
        key, label = _section(date_value, precision)
        current = result[-1] if result else None
        if current is None or current.key != key or current.count >= size:
            current = Block(
                index=len(result),
                key=key,
                label=label,
                header=current is None or current.key != key,
            )
            result.append(current)
        current.ids.append(pk)
        current.ratio += width / height if height else 1.0
    return result


def sections(all_blocks):
    """[(key, label, photo count)] for the year rail."""
    found = []
    for block in all_blocks:
        if block.header:
            found.append([block.key, block.label, 0])
        found[-1][2] += block.count
    return [tuple(section) for section in found]


def block_photos(block, user):
    """The photos of one block, in gallery order, ready for the justified grid."""
    by_pk = Photo.objects.select_related("event_type").in_bulk(block.ids)
    photos = [by_pk[pk] for pk in block.ids if pk in by_pk]
    pending = set(
        voting.photos_pending_for_user(user).filter(pk__in=block.ids).values_list("pk", flat=True)
    )
    asked = set(
        Question.objects.filter(photo_id__in=block.ids, status=Question.Status.OPEN).values_list(
            "photo_id", flat=True
        )
    )
    for photo in photos:
        photo.pending_my_vote = photo.pk in pending
        photo.has_open_questions = photo.pk in asked
    return photos
