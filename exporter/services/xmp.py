"""Build the exiftool arguments for an Immich-compatible XMP sidecar (spec section 6)."""

from catalog.models import Face, Photo

EVENT_TAG_ROOT = "Eventos"
LOCATION_TAG_ROOT = "Ubicaciones"
APPROXIMATE_DATE_TAG = "Fecha/aproximada"
NO_DATE_FOLDER = "sin-fecha"

# Characters with meaning in exiftool's serialized structure syntax.
_STRUCT_SPECIAL = set(",=[]{}|")


def escape_struct_value(value):
    """Escape a value for exiftool's flattened structure syntax (special chars get a '|')."""
    escaped = "".join(f"|{c}" if c in _STRUCT_SPECIAL else c for c in value)
    # Leading/trailing spaces would be trimmed by exiftool.
    if escaped[:1] == " ":
        escaped = "|" + escaped
    return escaped


def _tag_part(text):
    """A free-text value used inside a hierarchical tag ('/' separates levels)."""
    return " ".join(text.replace("/", "-").replace("|", "-").split())


def export_folder(photo):
    if photo.date_value and photo.date_precision in (
        Photo.DatePrecision.EXACT,
        Photo.DatePrecision.YEAR,
    ):
        return str(photo.date_value.year)
    return NO_DATE_FOLDER


def face_regions(photo):
    """Accepted faces as MWG regions with normalized, centered coordinates."""
    regions = []
    faces = photo.faces.filter(status=Face.Status.ACTIVE, person__isnull=False).select_related(
        "person"
    )
    for face in faces:
        person = face.person.resolved()
        regions.append(
            {
                "name": person.display_name,
                "x": round((face.x + face.w / 2) / photo.width, 6),
                "y": round((face.y + face.h / 2) / photo.height, 6),
                "w": round(face.w / photo.width, 6),
                "h": round(face.h / photo.height, 6),
            }
        )
    return regions


def tags(photo):
    result = []
    if photo.event_type_id:
        event_tag = f"{EVENT_TAG_ROOT}/{_tag_part(photo.event_type.name)}"
        result.append(event_tag)
        description = _tag_part(photo.event_description)
        if description:
            result.append(f"{event_tag}/{description}")
    if photo.location_id:
        result.append(f"{LOCATION_TAG_ROOT}/{_tag_part(photo.location.name)}")
    if photo.date_value and photo.date_precision in (
        Photo.DatePrecision.YEAR,
        Photo.DatePrecision.DECADE,
    ):
        result.append(APPROXIMATE_DATE_TAG)
    if photo.date_value and photo.date_precision == Photo.DatePrecision.DECADE:
        result.append(f"Fecha/Década de {photo.decade}")
    return result


def date_time_original(photo):
    """EXIF date when precise enough: exact date, or 1 January for a year."""
    if not photo.date_value:
        return None
    if photo.date_precision == Photo.DatePrecision.EXACT:
        return photo.date_value.strftime("%Y:%m:%d 00:00:00")
    if photo.date_precision == Photo.DatePrecision.YEAR:
        return f"{photo.date_value.year:04d}:01:01 00:00:00"
    return None


def region_info_value(photo, regions):
    items = ",".join(
        "{Area={"
        f"X={r['x']},Y={r['y']},W={r['w']},H={r['h']},Unit=normalized"
        "},"
        f"Name={escape_struct_value(r['name'])},Type=Face"
        "}"
        for r in regions
    )
    return (
        "{AppliedToDimensions={"
        f"W={photo.width},H={photo.height},Unit=pixel"
        "},RegionList=["
        f"{items}"
        "]}"
    )


def exiftool_args(photo, output_path):
    """Arguments that make exiftool create `output_path` (.xmp) from scratch."""
    args = ["-charset", "utf8"]
    regions = face_regions(photo)
    if regions:
        args.append(f"-XMP-mwg-rs:RegionInfo={region_info_value(photo, regions)}")
    for tag in tags(photo):
        args.append(f"-XMP-dc:Subject={tag}")
        args.append(f"-XMP-digiKam:TagsList={tag}")
    if photo.event_description:
        args.append(f"-XMP-dc:Description={photo.event_description}")
    if photo.location_id:
        args.append(f"-XMP-iptcCore:Location={photo.location.name}")
    date = date_time_original(photo)
    if date:
        args.append(f"-XMP-exif:DateTimeOriginal={date}")
    args += ["-o", str(output_path)]
    return args
