from io import BytesIO

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import (
    FileResponse,
    Http404,
    HttpResponse,
    JsonResponse,
    StreamingHttpResponse,
)
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
from catalog.services import avatars, person_details, sharing, similarity, voting
from scanner.services.images import photo_path, thumb_path

PERSON_PAGE_SIZE = 60
IMAGE_CACHE = "private, max-age=86400"


# ---------------------------------------------------------------------------
# Home, gallery, persons, activity
# ---------------------------------------------------------------------------


def home(request):
    photos = Photo.objects.visible()
    total = photos.count()
    cataloged = photos.filter(status=Photo.Status.CATALOGED).count()
    pending = presenters.with_tally(voting.pending_for_user(request.user), request.user)
    percent = round(100 * cataloged / total) if total else 0
    progress = None
    if request.user.is_player:
        from gamification.services import scoring

        progress = scoring.progress(request.user)
    return render(
        request,
        "catalog/home.html",
        {
            "total": total,
            "cataloged": cataloged,
            "percent": percent,
            "milestone": presenters.family_milestone(percent),
            "pending_count": pending.count(),
            "pending": pending.order_by("created_at")[:10],
            "progress": progress,
        },
    )


def gallery(request):
    """Skeleton of every block; the browser loads each block near the screen (?block=i)."""
    from catalog.services import gallery as gallery_service

    params = request.GET
    photos = gallery_service.gallery_queryset(params, request.user)
    all_blocks = gallery_service.blocks(photos)
    if request.htmx_partial and "block" in params:
        index = params.get("block", "")
        if not index.isdigit() or int(index) >= len(all_blocks):
            raise Http404
        block = all_blocks[int(index)]
        block.photos = gallery_service.block_photos(block, request.user)
        return render(request, "catalog/_gallery_block.html", {"block": block})

    # Sent filled with the page: the first blocks, or those around where the user was.
    anchor = params.get("anchor", "")
    anchor = min(int(anchor), len(all_blocks) - 1) if anchor.isdigit() and all_blocks else 0
    for block in all_blocks[max(0, anchor - 1) : anchor + 2]:
        block.photos = gallery_service.block_photos(block, request.user)

    people = list(Person.objects.active().filter(is_provisional=False))
    avatars = presenters.person_avatars([p.pk for p in people])
    nicknames = presenters.person_nicknames([p.pk for p in people])
    for person in people:
        person.avatar_id = avatars.get(person.pk)
        # The sheet's search also finds people by honorific and nicknames.
        person.search_text = " ".join([person.shown_name, *nicknames.get(person.pk, [])])
    chosen = gallery_service.selected_people(params)
    event_types = list(EventType.objects.filter(is_provisional=False))
    location = gallery_service.selected_location(params)
    decades = sorted(
        {d.year // 10 * 10 for d in Photo.objects.visible().dates("date_value", "year")},
        reverse=True,
    )

    def selected(options, key):
        value = params.get(key, "")
        return next((label for option, label in options if str(option) == value), None)

    query = params.copy()
    for name in ("block", "anchor", "page"):
        query.pop(name, None)
    return render(
        request,
        "catalog/gallery.html",
        {
            "params": params,
            "blocks": all_blocks,
            "sections": gallery_service.sections(all_blocks),
            "photo_count": sum(block.count for block in all_blocks),
            "anchor": anchor,
            "query": query.urlencode(),
            "statuses": Photo.Status.choices,
            "persons": people,
            "chosen_people": chosen,
            "chosen_ids": [person.pk for person in chosen],
            "event_types": event_types,
            "locations": gallery_service.location_options(),
            "decades": decades,
            "selected_status": selected(Photo.Status.choices, "status"),
            "selected_event": selected([(e.pk, str(e)) for e in event_types], "event"),
            "selected_location": location.name if location else None,
            "location_param": str(location.pk) if location else "",
            "selected_decade": selected([(d, f"{d}s") for d in decades], "decade"),
            "share_max": settings.SHARE_MAX_PHOTOS,
            "has_filters": bool(chosen)
            or any(params.get(k) for k in ("status", "event", "location", "decade", "mine")),
        },
    )


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
        # By name, honorific or nickname; a match by nickname is shown ("Tito Paco · Pico").
        found = []
        for person, nickname in person_details.search_persons(people, query):
            person.nickname_hint = nickname
            found.append(person)
        people = found
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
    page = Paginator(photos, PERSON_PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request,
        "catalog/person_detail.html",
        {
            "person": person,
            "page": page,
            "candidates": similarity.candidate_faces_for_person(person),
            "avatar_id": avatars.avatar_face_id(person),
            "avatar_choices": avatars.selectable_faces(person),
            "people_json": presenters.people_json(exclude=person.pk),
            **presenters.person_sheet_context(person),
        },
    )


@require_POST
def person_claim(request, pk):
    """«Soy yo»: the signed-in user identifies as this person."""
    from accounts.services.users import claim_person

    person = get_object_or_404(Person, pk=pk, merged_into__isnull=True)
    try:
        claim_person(request.user, person)
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    else:
        messages.success(
            request,
            _("Ahora eres %(name)s en el álbum. Elige tu foto de perfil en «Mi perfil».")
            % {"name": person.shown_name},
        )
    return redirect("person_detail", pk=person.pk)


@require_POST
def person_unclaim(request, pk):
    from accounts.services.users import unclaim_person

    person = get_object_or_404(Person, pk=pk)
    if person.user_id == request.user.pk:
        unclaim_person(request.user)
        messages.success(request, _("Ya no estás vinculado a %(name)s.") % {"name": person})
    return redirect("person_detail", pk=person.pk)


@admin_required
@require_POST
def person_rename(request, pk):
    from catalog.services.persons import rename_person

    person = get_object_or_404(Person, pk=pk, merged_into__isnull=True)
    try:
        rename_person(request.user, person, request.POST.get("display_name", ""))
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    else:
        messages.success(request, _("Nombre actualizado."))
    return redirect("person_detail", pk=person.pk)


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
    context = presenters.proposal_activity(
        Proposal.objects.filter(author=request.user), request.GET, request.user
    )
    context["clear_url"] = reverse("my_activity")
    return render(request, "catalog/my_activity.html", context)


# ---------------------------------------------------------------------------
# Photo screen
# ---------------------------------------------------------------------------


def _render_photo(request, photo, status=200):
    selected = request.GET.get("face") or request.POST.get("selected_face")
    context = presenters.photo_context(photo, request.user, selected_face=selected)
    context["event_types"] = voting.event_types()
    if not request.htmx_partial:
        context["people_json"] = presenters.people_json()
        context["locations_json"] = [
            {"name": name} for name in voting.locations().values_list("name", flat=True)
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
            # Chosen among the automatic suggestions: scores less (the AI helped).
            value={"suggested": True} if request.POST.get("suggested") else None,
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
def propose_location(request, pk):
    photo = get_object_or_404(Photo, pk=pk)
    return _run(
        request,
        photo,
        lambda: voting.create_proposal(
            request.user,
            photo,
            Proposal.Kind.LOCATION,
            location_name=request.POST.get("location", ""),
        ),
    )


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
            if request.POST.get("mode") == "quad":
                points = [
                    [float(request.POST[f"q{i}x"]), float(request.POST[f"q{i}y"])] for i in range(4)
                ]
                recrop.request_quad(request.user, photo, points)
            else:
                box = [float(request.POST[k]) for k in ("x0", "y0", "x1", "y1")]
                angle = float(request.POST["angle"])
                recrop.request_crop(request.user, photo, box, angle)
        except (KeyError, ValueError):
            error = _("No se puede recortar así: el recuadro se sale del escaneo o es muy pequeño.")
        else:
            messages.success(request, _("Recorte guardado. Se aplica en unos segundos."))
            return redirect("photo_detail", pk=photo.pk)
    rect = photo.crop_rect
    ready = recrop.editor_ready(photo)
    quad = None
    if ready:
        current, _size = recrop.photo_geometry(photo)
        quad = recrop.photo_order_to_view([list(p) for p in current], photo.rotation)
        # Phone photos of a print: start from the detected corners (only a suggestion).
        if not photo.crop_manual and recrop.is_single_upload(photo):
            quad = recrop.suggest_quad(photo) or quad
    return render(
        request,
        "catalog/crop.html",
        {
            "photo": photo,
            "ready": ready,
            "perspective": bool(photo.crop_quad),
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
                "quad": [[round(x, 1), round(y, 1)] for x, y in quad] if quad else None,
                "detectUrl": reverse("crop_detect", args=[photo.pk]),
            },
        },
        status=400 if error else 200,
    )


@admin_required
def crop_detect(request, pk):
    """Suggested corners of the print in the photo's scan (perspective mode)."""
    from scanner.services import recrop

    photo = get_object_or_404(Photo.objects.select_related("scan"), pk=pk)
    return JsonResponse({"quad": recrop.suggest_quad(photo)})


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
