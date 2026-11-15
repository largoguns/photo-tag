from django.conf import settings
from django.contrib import messages
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect, render

from accounts.permissions import admin_required
from catalog.models import Upload
from scanner.services.uploads import save_uploads


def upload(request):
    """Any signed-in user can upload scans or single photos for processing."""
    if request.method == "POST":
        kind = request.POST.get("kind", Upload.Kind.SCAN)
        results = save_uploads(request.user, request.FILES.getlist("files"), kind)
        if request.headers.get("Accept") == "application/json":
            return JsonResponse(
                {"results": [{"name": r.name, "ok": r.ok, "message": r.message} for r in results]}
            )
        for result in results:
            level = messages.success if result.ok else messages.error
            level(request, f"{result.name}: {result.message}")
        return redirect("upload")
    return render(
        request,
        "scanner/upload.html",
        {
            "kinds": Upload.Kind.choices,
            "max_mb": settings.UPLOAD_MAX_MB,
            "max_files": settings.UPLOAD_MAX_FILES,
            "my_uploads": Upload.objects.filter(user=request.user).select_related("scan")[:30],
        },
    )


def _scan_panel_context(request):
    from scanner.models import ScanJob

    latest = ScanJob.objects.select_related("upload__scan").first()
    photos = []
    if latest and latest.upload and latest.upload.scan_id:
        photos = list(latest.upload.scan.photos.filter(discarded=False))
    processing = bool(
        latest
        and latest.status == ScanJob.Status.DONE
        and latest.upload
        and latest.upload.status == Upload.Status.PENDING
    )
    return {
        "job": latest,
        "busy": bool(latest and latest.is_active),
        "processing": processing,
        "result_photos": photos,
        "resolutions": settings.SCAN_RESOLUTIONS,
        "default_resolution": settings.SCAN_DEFAULT_RESOLUTION,
    }


@admin_required
def scan(request):
    """Drive the network scanner: "Ready to scan?" → scan → result → ready again."""
    from scanner.models import ScanJob
    from scanner.services import scanning

    configured = scanning.is_configured()
    if request.method == "POST" and configured:
        try:
            resolution = int(request.POST.get("resolution", settings.SCAN_DEFAULT_RESOLUTION))
        except ValueError:
            resolution = 0
        if resolution not in settings.SCAN_RESOLUTIONS:
            return HttpResponseBadRequest("invalid resolution")
        if not ScanJob.objects.filter(
            status__in=[ScanJob.Status.QUEUED, ScanJob.Status.SCANNING]
        ).exists():
            scanning.request_scan(request.user, resolution)
        if not request.htmx_partial:
            return redirect("scan")
    context = _scan_panel_context(request) if configured else {}
    if request.htmx_partial:
        return render(request, "scanner/_scan_panel.html", context)
    context.update(
        {
            "configured": configured,
            "recent_jobs": ScanJob.objects.select_related("requested_by", "upload")[:10],
        }
    )
    return render(request, "scanner/scan.html", context)
