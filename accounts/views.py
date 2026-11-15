from django.conf import settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth import views as auth_views
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from accounts.forms import NewPasswordForm, ProfileForm, UserCreateForm, UserEditForm
from accounts.models import User
from accounts.permissions import superadmin_required
from accounts.services import audit_catalog
from accounts.services import users as user_service
from catalog.services import names
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
        elif "profile_face" in request.POST:
            try:
                user_service.set_profile_face(request.user, request.POST.get("profile_face"))
            except ValidationError as exc:
                messages.error(request, exc.messages[0])
            else:
                messages.success(request, _("Foto de perfil actualizada."))
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
            "progress": _progress(request.user),
            "my_person": getattr(request.user, "person", None),
            "profile_faces": user_service.profile_faces(request.user),
            "profile_face_id": user_service.avatar_face_id(request.user),
        },
    )


def _progress(user):
    if not user.is_player:
        return None
    from gamification.services import scoring

    return scoring.progress(user)


@superadmin_required
def user_list(request):
    query = request.GET.get("q", "").strip()
    users = User.objects.all().order_by("username")
    if query:
        users = names.search(users, query, "username", "display_name")
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


@superadmin_required
def audit(request):
    """Everything recorded, filterable by person, event and period (spec 5.7)."""
    from catalog import presenters
    from catalog.models import Proposal, Vote

    params = request.GET
    selected = params.get("user", "")
    person = User.objects.filter(pk=selected).first() if selected.isdigit() else None
    event = params.get("event", "")
    if event and audit_catalog.event_label(event) is None:
        event = ""
    period = params.get("period", "")
    if period not in audit_catalog.PERIODS:
        period = ""
    tab = params.get("tab", "history") if person else "history"
    if tab not in ("history", "proposals", "votes", "points"):
        tab = "history"

    users = list(User.objects.order_by("username"))
    context = {
        "params": params,
        "person": person,
        "tab": tab,
        "user_options": [(u.pk, f"{u.get_display_name()} (@{u.username})") for u in users],
        "period_options": [(key, label) for key, (label, _days) in audit_catalog.PERIODS.items()],
        "event_groups": audit_catalog.event_choices(),
        "selected_event": audit_catalog.event_label(event) if event else None,
        "selected_period": audit_catalog.PERIODS[period][0] if period else None,
        "has_filters": bool(person or event or period),
    }
    if person:
        context["summary"] = audit_catalog.summary(person)
        context["tab_urls"] = {
            name: f"{reverse('audit')}?user={person.pk}&tab={name}"
            for name in ("history", "proposals", "votes", "points")
        }
        context["progress"] = _progress(person)
    if tab == "proposals":
        context.update(
            presenters.proposal_activity(
                Proposal.objects.filter(author=person), params, request.user
            )
        )
        context["clear_url"] = context["tab_urls"]["proposals"]
        context["empty_text"] = _("No ha hecho propuestas.")
    elif tab == "points":
        from gamification.models import ScoreEvent
        from gamification.services.scoring import REASONS

        events = ScoreEvent.objects.filter(user=person)
        page = Paginator(events, 50).get_page(params.get("page"))
        for event in page:
            event.label = REASONS.get(event.reason, event.reason)
        context["page"] = page
    elif tab == "votes":
        votes = (
            Vote.objects.filter(user=person)
            .select_related("proposal__photo")
            .order_by("-created_at")
        )
        context["page"] = Paginator(votes, 50).get_page(params.get("page"))
    else:
        keys = audit_catalog.timeline_keys(person, event, period)
        page = Paginator(keys, 50).get_page(params.get("page"))
        context["page"] = page
        context["entries"] = audit_catalog.build_entries(page.object_list)
    return render(request, "accounts/audit.html", context)
