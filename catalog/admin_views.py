"""Administration screens (spec section 5.5, screen 8)."""

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from accounts.models import AuditLog
from accounts.permissions import admin_required
from accounts.services import audit
from catalog.forms import EventTypeForm, MergePersonsForm, ThresholdsForm
from catalog.models import CatalogSettings, EventType, Photo, Scan
from catalog.services.persons import merge_persons


@admin_required
def admin_home(request):
    return render(
        request,
        "catalog/admin/home.html",
        {
            "review_count": Scan.objects.filter(needs_review=True, reviewed=False).count(),
            "settings": CatalogSettings.load(),
            "recent_audit": AuditLog.objects.select_related("user")[:15],
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
        {"form": form, "event_types": EventType.objects.select_related("created_by")},
    )


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
    return redirect("admin_review_queue")


@admin_required
@require_POST
def photo_not_duplicate(request, pk):
    photo = get_object_or_404(Photo, pk=pk)
    original = photo.duplicate_of_id
    Photo.objects.filter(pk=pk).update(duplicate_of=None)
    audit.log(request.user, "photo.not_duplicate", photo, original=original)
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
                _("Personas fusionadas en «%(name)s».") % {"name": target.display_name},
            )
            return redirect("person_detail", pk=target.pk)
    return render(request, "catalog/admin/merge.html", {"form": form})
