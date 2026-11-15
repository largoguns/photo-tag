from io import BytesIO

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Case, Count, F, IntegerField, Q, Value, When
from django.http import FileResponse, Http404, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST
from PIL import Image

from accounts.permissions import admin_required
from accounts.services import audit
from catalog import presenters
from catalog.models import EventType, Face, Person, Photo, Proposal, Scan, Vote
from catalog.services import avatars, names, sharing, similarity, voting
from scanner.services.images import photo_path, thumb_path

GALLERY_PAGE_SIZE = 60
IMAGE_CACHE = "private, max-age=86400"


# ---------------------------------------------------------------------------
# Home, gallery, persons, activity
# ---------------------------------------------------------------------------


def home(request):
    photos = Photo.objects.visible()
    total = photos.count()
    cataloged = photos.filter(status=Photo.Status.CATALOGED).count()
    pending = presenters.with_tally(voting.pending_for_user(request.user), request.user)
    return render(
        request,
        "catalog/home.html",
        {
            "total": total,
            "cataloged": cataloged,
            "percent": round(100 * cataloged / total) if total else 0,
            "pending_count": pending.count(),
            "pending": pending.order_by("created_at")[:10],
        },
    )


def _gallery_queryset(request):
    # Newest year first; within a year, precise dates before decade-only ones; undated last.
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
    params = request.GET
    if params.get("status") in Photo.Status.values:
        photos = photos.filter(status=params["status"])
    if params.get("person", "").isdigit():
        photos = photos.filter(
            faces__person_id=params["person"], faces__status=Face.Status.ACTIVE
        ).distinct()
    if params.get("event", "").isdigit():
        photos = photos.filter(event_type_id=params["event"])
    if params.get("decade", "").isdigit():
        decade = int(params["decade"])
        photos = photos.filter(date_value__year__gte=decade, date_value__year__lt=decade + 10)
    if params.get("mine"):
        photos = photos.filter(pk__in=voting.photos_pending_for_user(request.user).values("pk"))
    return photos


def _gallery_page_context(request):
    page = Paginator(_gallery_queryset(request), GALLERY_PAGE_SIZE).get_page(
        request.GET.get("page")
    )
    ids = [photo.pk for photo in page]
    pending = set(
        voting.photos_pending_for_user(request.user).filter(pk__in=ids).values_list("pk", flat=True)
    )
    for photo in page:
        photo.pending_my_vote = photo.pk in pending
    groups = presenters.group_photos(page, request.GET.get("last_group", ""))
    query = request.GET.copy()
    for name in ("page", "last_group"):
        query.pop(name, None)
    return {
        "page": page,
        "groups": groups,
        "last_group": groups[-1]["key"] if groups else request.GET.get("last_group", ""),
        "query": query.urlencode(),
    }


def gallery(request):
    context = _gallery_page_context(request)
    if request.htmx_partial:
        return render(request, "catalog/_gallery_page.html", context)

    params = request.GET
    people = list(Person.objects.active().filter(is_provisional=False))
    avatars = presenters.person_avatars([p.pk for p in people])
    for person in people:
        person.avatar_id = avatars.get(person.pk)
    event_types = list(EventType.objects.filter(is_provisional=False))
    decades = sorted(
        {d.year // 10 * 10 for d in Photo.objects.visible().dates("date_value", "year")},
        reverse=True,
    )

    def selected(options, key):
        value = params.get(key, "")
        return next((label for option, label in options if str(option) == value), None)

    context.update(
        {
            "params": params,
            "statuses": Photo.Status.choices,
            "persons": people,
            "event_types": event_types,
            "decades": decades,
            "selected_status": selected(Photo.Status.choices, "status"),
            "selected_person": selected([(p.pk, p.display_name) for p in people], "person"),
            "selected_event": selected([(e.pk, str(e)) for e in event_types], "event"),
            "selected_decade": selected([(d, f"{d}s") for d in decades], "decade"),
            "share_max": settings.SHARE_MAX_PHOTOS,
            "has_filters": any(
                params.get(k) for k in ("status", "person", "event", "decade", "mine")
            ),
        }
    )
    return render(request, "catalog/gallery.html", context)


def persons(request):
    query = request.GET.get("q", "").strip()
    people = (
        Person.objects.active()
        .filter(is_provisional=False)
        .annotate(
            photo_count=Count(
                "faces__photo",
                filter=Q(faces__status=Face.Status.ACTIVE, faces__photo__discarded=False),
                distinct=True,
            )
        )
        .order_by("display_name", "id")
    )
    if query:
        people = names.search(people, query, "display_name")
    page = Paginator(people, 60).get_page(request.GET.get("page"))
    avatars = presenters.person_avatars([person.pk for person in page])
    for person in page:
        person.avatar_id = avatars.get(person.pk)
    return render(request, "catalog/persons.html", {"page": page, "query": query})


def person_detail(request, pk):
    person = get_object_or_404(Person, pk=pk)
    if person.merged_into_id:
        return redirect("person_detail", pk=person.resolved().pk)
    photos = (
        Photo.objects.visible()
        .filter(faces__person=person, faces__status=Face.Status.ACTIVE)
        .distinct()
        .order_by("pk")
    )
    page = Paginator(photos, GALLERY_PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request,
        "catalog/person_detail.html",
        {
            "person": person,
            "page": page,
            "candidates": similarity.candidate_faces_for_person(person),
            "avatar_id": avatars.avatar_face_id(person),
            "avatar_choices": avatars.selectable_faces(person),
        },
    )


@require_POST
def person_avatar(request, pk):
    person = get_object_or_404(Person, pk=pk, merged_into__isnull=True)
    try:
        avatars.set_avatar(request.user, person, request.POST.get("face"))
    except (ValueError, TypeError):
        messages.error(request, _("Esa cara no es de esta persona."))
    else:
        messages.success(request, _("Foto principal actualizada."))
    return redirect("person_detail", pk=person.pk)


def my_activity(request):
    proposals = presenters.with_tally(
        Proposal.objects.filter(author=request.user), request.user
    ).select_related("photo")
    params = request.GET
    status, kind = params.get("status", ""), params.get("kind", "")
    if status in Proposal.Status.values:
        proposals = proposals.filter(status=status)
    if kind in Proposal.Kind.values:
        proposals = proposals.filter(kind=kind)
    page = Paginator(proposals.order_by("-created_at"), 30).get_page(params.get("page"))
    return render(
        request,
        "catalog/my_activity.html",
        {
            "page": page,
            "params": params,
            "statuses": Proposal.Status.choices,
            "kinds": Proposal.Kind.choices,
            "selected_status": Proposal.Status(status).label
            if status in Proposal.Status.values
            else None,
            "selected_kind": Proposal.Kind(kind).label if kind in Proposal.Kind.values else None,
        },
    )


# ---------------------------------------------------------------------------
# Photo screen
# ---------------------------------------------------------------------------


def _render_photo(request, photo, status=200):
    selected = request.GET.get("face") or request.POST.get("selected_face")
    context = presenters.photo_context(photo, request.user, selected_face=selected)
    context["event_types"] = voting.event_types()
    if not request.htmx_partial:
        people = list(
            Person.objects.active().order_by("display_name").values_list("pk", "display_name")
        )
        avatars = presenters.person_avatars([pk for pk, _name in people])
        face_url = reverse("face_image", args=[0]).replace("/0/", "/{id}/")
        # Used by the name autocomplete; avatars are face crops behind the login.
        context["people_json"] = [
            {
                "name": name,
                "avatar": face_url.replace("{id}", str(avatars[pk])) if pk in avatars else "",
            }
            for pk, name in people
        ]
    context["date_precisions"] = Photo.DatePrecision.choices
    template = "catalog/_photo_workspace.html" if request.htmx_partial else "catalog/photo.html"
    return render(request, template, context, status=status)


def photo_detail(request, pk):
    return _render_photo(request, get_object_or_404(Photo, pk=pk))


def next_uncataloged(request):
    after = request.GET.get("after")
    photo = presenters.next_uncataloged(int(after) if after and after.isdigit() else None)
    if photo is None:
        messages.success(request, _("¡No quedan fotos sin catalogar!"))
        return redirect("home")
    return redirect("photo_detail", pk=photo.pk)


def _after_action(request, photo, error=None):
    if error:
        messages.error(request, error)
    next_url = request.POST.get("next")
    if next_url and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(next_url)
    if request.htmx_partial:
        return _render_photo(request, Photo.objects.get(pk=photo.pk))
    url = reverse("photo_detail", args=[photo.pk])
    selected = request.POST.get("selected_face")
    return redirect(f"{url}?face={selected}" if selected else url)


def _run(request, photo, action):
    try:
        action()
    except ValidationError as exc:
        return _after_action(request, photo, error=" ".join(exc.messages))
    return _after_action(request, photo)


@require_POST
def propose_identity(request, face_pk):
    face = get_object_or_404(Face, pk=face_pk)
    person_id = request.POST.get("person_id", "")
    person = (
        get_object_or_404(Person.objects.active(), pk=person_id) if person_id.isdigit() else None
    )
    return _run(
        request,
        face.photo,
        lambda: voting.create_proposal(
            request.user,
            face.photo,
            Proposal.Kind.FACE_IDENTITY,
            face=face,
            person=person,
            person_name=request.POST.get("name", ""),
        ),
    )


@require_POST
def propose_remove_face(request, face_pk):
    face = get_object_or_404(Face, pk=face_pk)
    return _run(
        request,
        face.photo,
        lambda: voting.create_proposal(
            request.user, face.photo, Proposal.Kind.REMOVE_FACE, face=face
        ),
    )


@require_POST
def propose_add_face(request, pk):
    photo = get_object_or_404(Photo, pk=pk)
    box = {k: request.POST.get(k) for k in ("x", "y", "w", "h")}
    return _run(
        request,
        photo,
        lambda: voting.create_proposal(request.user, photo, Proposal.Kind.ADD_FACE, value=box),
    )


@require_POST
def propose_event(request, pk):
    photo = get_object_or_404(Photo, pk=pk)

    def action():
        # "Other…" with a name proposes a new event type; a typed name always wins.
        event_name = request.POST.get("event_name", "").strip()
        selected = request.POST.get("event_type", "")
        event_type = None
        if not event_name and selected.isdigit():
            event_type = EventType.objects.enabled().filter(pk=selected).first()
        voting.create_proposal(
            request.user,
            photo,
            Proposal.Kind.EVENT,
            event_type=event_type,
            event_name=event_name,
            value={"description": request.POST.get("description", "")},
        )

    return _run(request, photo, action)


@require_POST
def propose_date(request, pk):
    photo = get_object_or_404(Photo, pk=pk)

    def action():
        precision = request.POST.get("precision")
        value = {"precision": precision, "year": request.POST.get("year")}
        if precision == Photo.DatePrecision.EXACT:
            parts = (request.POST.get("date") or "").split("-")
            if len(parts) != 3:
                raise ValidationError(_("Indica la fecha completa."))
            value.update(year=parts[0], month=parts[1], day=parts[2])
        voting.create_proposal(request.user, photo, Proposal.Kind.DATE, value=value)

    return _run(request, photo, action)


def download_photos(request):
    """One photo as JPEG or several as a ZIP (when the browser cannot share files)."""
    photos = sharing.photos_for(sharing.parse_ids(request.GET.get("ids")))
    if not photos:
        raise Http404
    audit.log(request.user, "photo.download", None, count=len(photos), ids=[p.pk for p in photos])
    if len(photos) == 1:
        response = FileResponse(
            photo_path(photos[0].file_name).open("rb"),
            content_type="image/jpeg",
            as_attachment=True,
            filename=sharing.download_name(photos[0]),
        )
    else:
        response = StreamingHttpResponse(sharing.zip_stream(photos), content_type="application/zip")
        response["Content-Disposition"] = 'attachment; filename="PhotoTag.zip"'
    response["Cache-Control"] = "private, no-store"
    return response


@require_POST
def log_share(request):
    """Audit trail for photos sent with the device's share sheet."""
    ids = [p.pk for p in sharing.photos_for(sharing.parse_ids(request.POST.get("ids")))]
    if ids:
        audit.log(request.user, "photo.share", None, count=len(ids), ids=ids)
    return HttpResponse(status=204)


@require_POST
def rotate_photo(request, pk):
    from scanner.services.rotation import request_rotation

    photo = get_object_or_404(Photo, pk=pk)
    request_rotation(request.user, photo)
    return _after_action(request, photo)


@admin_required
def crop_photo(request, pk):
    """Editor to move the edges of a photo over its scan and straighten it."""
    from scanner.services import recrop

    photo = get_object_or_404(Photo.objects.select_related("scan"), pk=pk)
    error = None
    if request.method == "POST":
        try:
            box = [float(request.POST[k]) for k in ("x0", "y0", "x1", "y1")]
            angle = float(request.POST["angle"])
            recrop.request_crop(request.user, photo, box, angle)
        except (KeyError, ValueError):
            error = _("No se puede recortar así: el recuadro se sale del escaneo o es muy pequeño.")
        else:
            messages.success(request, _("Recorte guardado. Se aplica en unos segundos."))
            return redirect("photo_detail", pk=photo.pk)
    rect = photo.crop_rect
    return render(
        request,
        "catalog/crop.html",
        {
            "photo": photo,
            "ready": recrop.editor_ready(photo),
            "error": error,
            "crop": {
                "cx": rect.get("cx"),
                "cy": rect.get("cy"),
                "w": rect.get("w"),
                "h": rect.get("h"),
                "angle": rect.get("angle", 0),
                "inset": recrop.inset(photo) if rect.get("w") else 0,
                "rotation": photo.rotation,
                "width": photo.width,
                "height": photo.height,
                "scanWidth": photo.scan.width,
                "scanHeight": photo.scan.height,
                "maxAngle": recrop.MAX_STRAIGHTEN,
                "minSide": recrop.MIN_SIDE,
            },
        },
        status=400 if error else 200,
    )


@admin_required
def scan_preview(request, pk):
    from scanner.services.images import preview_path

    scan = get_object_or_404(Scan, pk=pk)
    path = preview_path(scan.sha256)
    if not path.exists():
        raise Http404
    response = FileResponse(path.open("rb"), content_type="image/jpeg")
    response["Cache-Control"] = IMAGE_CACHE
    return response


@require_POST
def vote(request, pk):
    proposal = get_object_or_404(Proposal, pk=pk)
    value = Vote.UP if request.POST.get("value") == "up" else Vote.DOWN
    return _run(request, proposal.photo, lambda: voting.cast_vote(request.user, proposal, value))


@admin_required
@require_POST
def decide(request, pk):
    proposal = get_object_or_404(Proposal, pk=pk)
    accept = request.POST.get("decision") == "accept"
    return _run(
        request, proposal.photo, lambda: voting.admin_decide(request.user, proposal, accept)
    )


# ---------------------------------------------------------------------------
# Images: always through an authenticated view, never as public static files
# ---------------------------------------------------------------------------


def photo_image(request, pk, size):
    photo = get_object_or_404(Photo, pk=pk)
    if size == "full":
        path = photo_path(photo.file_name)
    elif size in ("400", "1600"):
        path = thumb_path(photo.file_name, int(size))
        if not path.exists():
            path = photo_path(photo.file_name)
    else:
        raise Http404
    if not path.exists():
        raise Http404
    response = FileResponse(path.open("rb"), content_type="image/jpeg")
    response["Cache-Control"] = IMAGE_CACHE
    return response


def face_image(request, pk):
    """Square-ish crop around a face, used as person avatar."""
    face = get_object_or_404(Face.objects.select_related("photo"), pk=pk)
    photo = face.photo
    source = thumb_path(photo.file_name, 1600)
    if not source.exists():
        source = photo_path(photo.file_name)
    if not source.exists():
        raise Http404
    with Image.open(source) as img:
        scale = img.width / photo.width
        margin = 0.3 * max(face.w, face.h)
        box = (
            max(0, (face.x - margin) * scale),
            max(0, (face.y - margin) * scale),
            min(img.width, (face.x + face.w + margin) * scale),
            min(img.height, (face.y + face.h + margin) * scale),
        )
        avatar = img.convert("RGB").crop(tuple(round(v) for v in box))
        avatar.thumbnail((240, 240))
        buffer = BytesIO()
        avatar.save(buffer, "JPEG", quality=85)
    response = HttpResponse(buffer.getvalue(), content_type="image/jpeg")
    # Short cache: the crop changes when the photo is rotated or the box is moved.
    response["Cache-Control"] = "private, max-age=300"
    return response
