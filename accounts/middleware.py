from django.shortcuts import redirect
from django.urls import resolve

# URL names a user with a pending password change can still reach.
PASSWORD_CHANGE_ALLOWED = {"password_change_forced", "logout", "login", "healthz"}


class ForcePasswordChangeMiddleware:
    """Block every screen until a user with the generic password chooses a new one."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and user.must_change_password:
            match = resolve(request.path_info) if request.path_info else None
            if match is None or match.url_name not in PASSWORD_CHANGE_ALLOWED:
                return redirect("password_change_forced")
        return self.get_response(request)
