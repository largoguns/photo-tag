from django.conf import settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth import views as auth_views
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from accounts.forms import NewPasswordForm, ProfileForm, UserCreateForm, UserEditForm
from accounts.models import User
from accounts.permissions import superadmin_required
from accounts.services import users as user_service
from notifications.forms import NotificationSettingsForm
from notifications.models import NotificationSettings
from notifications.services import push


class LoginView(auth_views.LoginView):
    template_name = "accounts/login.html"
    redirect_authenticated_user = True


def password_change_forced(request):
    """First-login screen: the user must replace the generic password."""
    form = NewPasswordForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user_service.change_own_password(request.user, form.cleaned_data["new_password1"])
        update_session_auth_hash(request, request.user)
        messages.success(request, _("Contraseña actualizada."))
        return redirect("home")
    return render(request, "accounts/password_change_forced.html", {"form": form})


def profile(request):
    profile_form = ProfileForm(instance=request.user)
    password_form = NewPasswordForm(request.user)
    notification_settings = NotificationSettings.for_user(request.user)
    notification_form = NotificationSettingsForm(instance=notification_settings)
    if request.method == "POST":
        if "save_notifications" in request.POST:
            notification_form = NotificationSettingsForm(
                request.POST, instance=notification_settings
            )
            if notification_form.is_valid():
                notification_form.save()
                messages.success(request, _("Preferencias de avisos guardadas."))
                return redirect("profile")
        elif "save_profile" in request.POST:
            profile_form = ProfileForm(request.POST, instance=request.user)
            if profile_form.is_valid():
                profile_form.save()
                messages.success(request, _("Perfil actualizado."))
                return redirect("profile")
        elif "change_password" in request.POST:
            password_form = NewPasswordForm(request.user, request.POST)
            if password_form.is_valid():
                user_service.change_own_password(
                    request.user, password_form.cleaned_data["new_password1"]
                )
                update_session_auth_hash(request, request.user)
                messages.success(request, _("Contraseña actualizada."))
                return redirect("profile")
    return render(
        request,
        "accounts/profile.html",
        {
            "profile_form": profile_form,
            "password_form": password_form,
            "notification_form": notification_form,
            "push_enabled": push.is_enabled(),
            "vapid_public_key": settings.VAPID_PUBLIC_KEY,
            "push_devices": request.user.push_subscriptions.all(),
        },
    )


@superadmin_required
def user_list(request):
    query = request.GET.get("q", "").strip()
    users = User.objects.all().order_by("username")
    if query:
        users = users.filter(Q(username__icontains=query) | Q(display_name__icontains=query))
    page = Paginator(users, 50).get_page(request.GET.get("page"))
    return render(request, "accounts/user_list.html", {"page": page, "query": query})


@superadmin_required
def user_create(request):
    form = UserCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            user = user_service.create_user(
                request.user,
                username=form.cleaned_data["username"],
                display_name=form.cleaned_data["display_name"],
                role=form.cleaned_data["role"],
                password=form.cleaned_data["password"] or None,
            )
        except Exception as exc:  # misconfiguration (no generic password) or validation
            form.add_error(None, str(exc))
        else:
            messages.success(
                request, _("Usuario «%(username)s» creado.") % {"username": user.username}
            )
            return redirect("user_list")
    return render(request, "accounts/user_form.html", {"form": form, "creating": True})


@superadmin_required
def user_edit(request, pk):
    target = get_object_or_404(User, pk=pk)
    linked = getattr(target, "person", None)
    initial = {"display_name": target.display_name, "role": target.role, "person": linked}
    form = UserEditForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            user_service.update_user(
                request.user,
                target,
                display_name=form.cleaned_data["display_name"],
                role=form.cleaned_data["role"],
            )
            if form.cleaned_data["person"] != linked:
                user_service.link_person(request.user, target, form.cleaned_data["person"])
        except ValidationError as exc:
            form.add_error("role", exc.messages[0])
        else:
            messages.success(request, _("Usuario actualizado."))
            return redirect("user_list")
    return render(
        request, "accounts/user_form.html", {"form": form, "target": target, "creating": False}
    )


@superadmin_required
@require_POST
def user_toggle_active(request, pk):
    target = get_object_or_404(User, pk=pk)
    try:
        user_service.set_active(request.user, target, not target.is_active)
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    else:
        message = (
            _("Usuario «%(username)s» reactivado.")
            if not target.is_active
            else _("Usuario «%(username)s» desactivado.")
        )
        messages.success(request, message % {"username": target.username})
    return redirect("user_list")


@superadmin_required
@require_POST
def user_reset_password(request, pk):
    target = get_object_or_404(User, pk=pk)
    try:
        user_service.reset_password(request.user, target)
    except Exception as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            _("Contraseña de «%(username)s» restablecida a la contraseña genérica.")
            % {"username": target.username},
        )
    return redirect("user_list")
