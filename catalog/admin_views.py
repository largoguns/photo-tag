"""Administration screens (spec section 5.5, screen 8)."""

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from accounts.permissions import admin_required
from accounts.services import audit, audit_catalog
from catalog.forms import EventTypeForm, MergePersonsForm, ThresholdsForm
from catalog.models import CatalogSettings, EventType, Location, Photo, Scan
from catalog.services import catalog_review
from catalog.services import locations as location_service
from catalog.services.persons import merge_persons


@admin_required
def admin_home(request):
    return render(
        request,
        "catalog/admin/home.html",
        {
            "review_count": Scan.objects.filter(needs_review=True, reviewed=False).count(),
            "events_to_review": EventType.objects.filter(is_provisional=True).count(),
            "locations_to_review": Location.objects.filter(
                is_provisional=True, merged_into__isnull=True
            ).count(),
            "settings": CatalogSettings.load(),
            "recent_audit": audit_catalog.recent(15),
            "scanner_configured": bool(settings.SCANNER_URL),
        },
    )


@admin_required
def event_types(request):
    form = EventTypeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        event_type = form.save()
        audit.log(request.user, "event_type.create", event_type, name=event_type.name)
        messages.success(
            request, _("Tipo de evento «%(name)s» creado.") % {"name": event_type.name}
        )
        return redirect("admin_event_types")
    return render(
        request,
        "catalog/admin/event_types.html",
        {
            "form": form,
            "event_types": EventType.objects.filter(is_provisional=False),
            "to_review": _with_example(catalog_review.pending_event_types()),
        },
    )


def _with_example(entries):
    entries = list(entries)
    for entry in entries:
        entry.example_photo = catalog_review.first_photo_id(entry)
    return entries


def _review(request, entry, back):
    """Accept (as typed or rewritten), reject or — events — turn into a location."""
    name = entry.name
    action = request.POST.get("action")
    try:
        if action == "accept":
            accepted = catalog_review.accept(request.user, entry, request.POST.get("text", ""))
            messages.success(request, _("«%(name)s» aceptado.") % {"name": accepted.name})
        elif action == "reject":
            catalog_review.reject(request.user, entry)
            messages.success(request, _("«%(name)s» rechazado.") % {"name": name})
        elif action == "location" and isinstance(entry, EventType):
            location = catalog_review.to_location(request.user, entry)
            messages.success(
                request,
                _("«%(name)s» es ahora la ubicación «%(location)s».")
                % {"name": name, "location": location.name},
            )
        else:
            messages.error(request, _("Acción no válida."))
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect(back)


@admin_required
@require_POST
def event_type_review(request, pk):
    entry = get_object_or_404(EventType, pk=pk, is_provisional=True)
    return _review(request, entry, "admin_event_types")


@admin_required
@require_POST
def location_review(request, pk):
    entry = get_object_or_404(Location, pk=pk, is_provisional=True, merged_into__isnull=True)
    return _review(request, entry, "admin_locations")


@admin_required
def event_type_edit(request, pk):
    event_type = get_object_or_404(EventType, pk=pk)
    form = EventTypeForm(request.POST or None, instance=event_type)
    if request.method == "POST" and form.is_valid():
        form.save()
        audit.log(request.user, "event_type.update", event_type, **form.cleaned_data)
        messages.success(request, _("Tipo de evento actualizado."))
        return redirect("admin_event_types")
    return render(
        request, "catalog/admin/event_type_form.html", {"form": form, "event_type": event_type}
    )


def _with_photo_count(queryset):
    return queryset.annotate(photo_count=Count("photos", filter=Q(photos__discarded=False)))


@admin_required
def locations(request):
    rows = _with_photo_count(
        Location.objects.filter(merged_into__isnull=True, is_provisional=False).select_related(
            "created_by"
        )
    )
    return render(
        request,
        "catalog/admin/locations.html",
        {"locations": rows, "to_review": _with_example(catalog_review.pending_locations())},
    )


@admin_required
def location_edit(request, pk):
    location = get_object_or_404(_with_photo_count(Location.objects.all()), pk=pk)
    if location.merged_into_id:
        return redirect("admin_location_edit", pk=location.resolved().pk)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "rename":
                location_service.rename_location(
                    request.user, location, request.POST.get("name", "")
                )
                messages.success(request, _("Ubicación renombrada."))
            elif action in ("activate", "deactivate"):
                location_service.set_active(request.user, location, action == "activate")
                messages.success(request, _("Ubicación actualizada."))
            elif action == "merge":
                target = get_object_or_404(
                    Location.objects.filter(merged_into__isnull=True).exclude(pk=location.pk),
                    pk=request.POST.get("target") or 0,
                )
                location_service.merge_locations(request.user, location, target)
                messages.success(
                    request, _("Ubicaciones fusionadas en «%(name)s».") % {"name": target.name}
                )
                return redirect("admin_location_edit", pk=target.pk)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        return redirect("admin_location_edit", pk=location.pk)
    others = Location.objects.filter(merged_into__isnull=True).exclude(pk=location.pk)
    return render(
        request,
        "catalog/admin/location_form.html",
        {"location": location, "others": others},
    )


@admin_required
def thresholds(request):
    conf = CatalogSettings.load()
    form = ThresholdsForm(request.POST or None, instance=conf)
    if request.method == "POST" and form.is_valid():
        form.save()
        audit.log(request.user, "settings.thresholds", conf, **form.cleaned_data)
        messages.success(request, _("Umbrales actualizados."))
        return redirect("admin_home")
    return render(request, "catalog/admin/thresholds.html", {"form": form})


@admin_required
def review_queue(request):
    scans = Scan.objects.filter(needs_review=True, reviewed=False).prefetch_related(
        "photos__duplicate_of"
    )
    return render(request, "catalog/admin/review_queue.html", {"scans": scans})


@admin_required
@require_POST
def review_mark_ok(request, pk):
    scan = get_object_or_404(Scan, pk=pk)
    scan.reviewed = True
    scan.save(update_fields=["reviewed"])
    audit.log(request.user, "scan.reviewed", scan)
    messages.success(
        request, _("Escaneo «%(name)s» marcado como revisado.") % {"name": scan.file_name}
    )
    return redirect("admin_review_queue")


@admin_required
@require_POST
def photo_toggle_discard(request, pk):
    photo = get_object_or_404(Photo, pk=pk)
    photo.discarded = not photo.discarded
    photo.save(update_fields=["discarded"])
    audit.log(request.user, "photo.discard" if photo.discarded else "photo.restore", photo)
    from gamification.services import scoring

    if photo.discarded:
        scoring.photo_removed(photo)
    else:
        scoring.photo_added(photo)
    return redirect("admin_review_queue")


def _safe_next(request):
    target = request.POST.get("next", "")
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return None


@admin_required
@require_POST
def photo_delete(request, pk):
    from scanner.services import deletion

    photo = get_object_or_404(Photo.objects.select_related("scan"), pk=pk)
    after = deletion.next_after(photo)
    deletion.delete_photo(request.user, photo)
    messages.success(request, _("Foto borrada."))
    if target := _safe_next(request):
        return redirect(target)
    return redirect(reverse("photo_detail", args=[after.pk]) if after else reverse("gallery"))


@admin_required
@require_POST
def scan_delete(request, pk):
    from scanner.services import deletion

    scan = get_object_or_404(Scan, pk=pk)
    name = scan.file_name
    deletion.delete_scan(request.user, scan)
    messages.success(
        request,
        _("Escaneo «%(name)s» borrado. Puedes volver a escanearlo o subirlo.") % {"name": name},
    )
    return redirect(_safe_next(request) or reverse("gallery"))


@admin_required
@require_POST
def scan_reprocess(request, pk):
    from scanner.services import deletion

    scan = get_object_or_404(Scan, pk=pk)
    deletion.request_reprocess(request.user, scan)
    messages.success(
        request, _("Reprocesando «%(name)s»: tardará unos segundos.") % {"name": scan.file_name}
    )
    return redirect(_safe_next(request) or reverse("admin_review_queue"))


@admin_required
@require_POST
def photo_not_duplicate(request, pk):
    photo = get_object_or_404(Photo, pk=pk)
    original = photo.duplicate_of_id
    Photo.objects.filter(pk=pk).update(duplicate_of=None)
    audit.log(request.user, "photo.not_duplicate", photo, original=original)
    from gamification.services import scoring

    photo.duplicate_of_id = None
    scoring.photo_added(photo)
    return redirect("admin_review_queue")


@admin_required
def merge(request):
    form = MergePersonsForm(request.POST or None, initial={"source": request.GET.get("source")})
    if request.method == "POST" and form.is_valid():
        try:
            target = merge_persons(
                request.user, form.cleaned_data["source"], form.cleaned_data["target"]
            )
        except ValidationError as exc:
            form.add_error(None, " ".join(exc.messages))
        else:
            messages.success(
                request,
                _("Personas fusionadas en «%(name)s».") % {"name": target.shown_name},
            )
            return redirect("person_detail", pk=target.pk)
    return render(request, "catalog/admin/merge.html", {"form": form})
