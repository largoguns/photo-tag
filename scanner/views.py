from django.conf import settings
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render

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
