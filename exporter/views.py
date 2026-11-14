from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from accounts.permissions import admin_required
from exporter.models import ExportRequest
from exporter.services.export import photos_to_export, request_export


@admin_required
def export_home(request):
    return render(
        request,
        "exporter/export.html",
        {
            "pending_photos": photos_to_export().count(),
            "requests": ExportRequest.objects.select_related("requested_by")[:20],
        },
    )


@admin_required
@require_POST
def export_start(request):
    if ExportRequest.objects.filter(
        status__in=[ExportRequest.Status.PENDING, ExportRequest.Status.RUNNING]
    ).exists():
        messages.info(request, "Ya hay una exportación en cola.")
    else:
        request_export(request.user)
        messages.success(request, "Exportación solicitada. El worker la ejecutará en breve.")
    return redirect("export_home")
