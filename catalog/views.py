from io import BytesIO

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST
from PIL import Image

from accounts.permissions import admin_required
from catalog import presenters
from catalog.models import EventType, Face, Person, Photo, Proposal, Vote
from catalog.services import similarity, voting
from scanner.services.images import photo_path, thumb_path

GALLERY_PAGE_SIZE = 24
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
    photos = Photo.objects.visible().order_by("pk")
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


def gallery(request):
    page = Paginator(_gallery_queryset(request), GALLERY_PAGE_SIZE).get_page(
        request.GET.get("page")
    )
    query = request.GET.copy()
    query.pop("page", None)
    context = {"page": page, "query": query.urlencode()}
    if request.htmx_partial:
        return render(request, "catalog/_gallery_page.html", context)

    decades = sorted(
        {d.year // 10 * 10 for d in Photo.objects.visible().dates("date_value", "year")}
    )
    context.update(
        {
            "statuses": Photo.Status.choices,
            "persons": Person.objects.active().filter(is_provisional=False),
            "event_types": EventType.objects.all(),
            "decades": decades,
            "filters": request.GET,
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
        people = people.filter(display_name__icontains=query)
    page = Paginator(people, 60).get_page(request.GET.get("page"))
    for person in page:
        person.avatar = person.faces.filter(status=Face.Status.ACTIVE).order_by("-w").first()
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
        },
    )


def my_activity(request):
    proposals = presenters.with_tally(
        Proposal.objects.filter(author=request.user), request.user
    ).select_related("photo")
    status = request.GET.get("status")
    if status in Proposal.Status.values:
        proposals = proposals.filter(status=status)
    page = Paginator(proposals.order_by("-created_at"), 30).get_page(request.GET.get("page"))
    return render(
        request,
        "catalog/my_activity.html",
        {"page": page, "statuses": Proposal.Status.choices, "status": status},
    )


# ---------------------------------------------------------------------------
# Photo screen
# ---------------------------------------------------------------------------


def _render_photo(request, photo, status=200):
    selected = request.GET.get("face") or request.POST.get("selected_face")
    context = presenters.photo_context(photo, request.user, selected_face=selected)
    context["event_types"] = voting.event_types()
    context["person_names"] = (
        Person.objects.active().order_by("display_name").values_list("display_name", flat=True)
    )
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
        event_type = EventType.objects.filter(pk=request.POST.get("event_type") or 0).first()
        voting.create_proposal(
            request.user,
            photo,
            Proposal.Kind.EVENT,
            event_type=event_type,
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
    response["Cache-Control"] = IMAGE_CACHE
    return response
