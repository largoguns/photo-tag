from functools import wraps

from django.core.exceptions import PermissionDenied


def admin_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_catalog_admin:
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapper


def superadmin_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_superadmin:
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapper
