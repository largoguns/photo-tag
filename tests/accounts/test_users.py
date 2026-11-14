import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db.models import ProtectedError
from django.urls import reverse

from accounts.models import AuditLog, Role, User
from accounts.services import users as user_service

pytestmark = pytest.mark.django_db

GENERIC = "Generica-2024!"


def login(client, username, password):
    return client.post(reverse("login"), {"username": username, "password": password})


# --- Superadmin bootstrap ---------------------------------------------------


def test_ensure_superadmin_creates_it_once(settings):
    settings.SUPERADMIN_USERNAME = "jefe"
    settings.SUPERADMIN_PASSWORD = "Superadmin-2024!"

    call_command("ensure_superadmin")
    settings.SUPERADMIN_PASSWORD = "otra-cosa-distinta"
    call_command("ensure_superadmin")

    user = User.objects.get(username="jefe")
    assert user.role == Role.SUPERADMIN
    assert user.check_password("Superadmin-2024!")
    assert User.objects.filter(role=Role.SUPERADMIN).count() == 1


def test_ensure_superadmin_requires_password(settings):
    settings.SUPERADMIN_PASSWORD = ""
    with pytest.raises(ImproperlyConfigured):
        user_service.ensure_superadmin()


# --- Forced password change -------------------------------------------------


def test_new_user_must_change_generic_password(client, superadmin):
    """Acceptance criterion 5."""
    user = user_service.create_user(superadmin, username="nuevo", display_name="Nuevo")
    assert user.must_change_password
    assert user.check_password(GENERIC)

    response = login(client, "nuevo", GENERIC)
    assert response.status_code == 302

    for url in [reverse("home"), reverse("gallery"), reverse("persons"), reverse("profile")]:
        response = client.get(url)
        assert response.status_code == 302
        assert response.url == reverse("password_change_forced")


def test_generic_password_cannot_be_kept(client, superadmin):
    user_service.create_user(superadmin, username="nuevo")
    login(client, "nuevo", GENERIC)

    response = client.post(
        reverse("password_change_forced"),
        {"new_password1": GENERIC, "new_password2": GENERIC},
    )

    assert response.status_code == 200
    assert User.objects.get(username="nuevo").must_change_password


def test_short_password_is_rejected(client, superadmin):
    user_service.create_user(superadmin, username="nuevo")
    login(client, "nuevo", GENERIC)

    response = client.post(
        reverse("password_change_forced"),
        {"new_password1": "Corta-1", "new_password2": "Corta-1"},
    )

    assert response.status_code == 200
    assert User.objects.get(username="nuevo").must_change_password


def test_changing_password_unlocks_the_app(client, superadmin):
    user_service.create_user(superadmin, username="nuevo")
    login(client, "nuevo", GENERIC)

    response = client.post(
        reverse("password_change_forced"),
        {"new_password1": "MiClaveFamiliar-77", "new_password2": "MiClaveFamiliar-77"},
    )

    assert response.status_code == 302
    user = User.objects.get(username="nuevo")
    assert not user.must_change_password
    assert client.get(reverse("home")).status_code == 200


def test_reset_password_restores_generic_and_forces_change(superadmin, alice):
    user_service.reset_password(superadmin, alice)

    alice.refresh_from_db()
    assert alice.check_password(GENERIC)
    assert alice.must_change_password
    assert AuditLog.objects.filter(action="user.reset_password", target_id=str(alice.pk)).exists()


def test_custom_initial_password(superadmin):
    user = user_service.create_user(superadmin, username="nuevo", password="Inicial-Propia-1")
    assert user.check_password("Inicial-Propia-1")
    assert user.must_change_password


# --- Deactivation and last superadmin protection ------------------------------


def test_deactivated_user_cannot_log_in(client, superadmin, alice):
    user_service.set_active(superadmin, alice, False)

    response = login(client, "alice", "Contrasena-segura-1")

    assert response.status_code == 200  # form redisplayed with an error
    assert "_auth_user_id" not in client.session


def test_deactivate_and_reactivate(superadmin, alice):
    user_service.set_active(superadmin, alice, False)
    alice.refresh_from_db()
    assert not alice.is_active
    user_service.set_active(superadmin, alice, True)
    alice.refresh_from_db()
    assert alice.is_active


def test_last_superadmin_cannot_be_deactivated(superadmin):
    with pytest.raises(user_service.LastSuperadminError):
        user_service.set_active(superadmin, superadmin, False)


def test_last_superadmin_cannot_be_demoted(superadmin):
    with pytest.raises(user_service.LastSuperadminError):
        user_service.update_user(superadmin, superadmin, display_name="", role=Role.ADMIN)


def test_last_superadmin_cannot_be_deleted(superadmin):
    with pytest.raises(user_service.LastSuperadminError):
        user_service.delete_user(superadmin, superadmin)
    with pytest.raises(ProtectedError):
        superadmin.delete()


def test_superadmin_can_be_demoted_if_another_exists(superadmin):
    other = user_service.create_user(superadmin, username="otro", role=Role.SUPERADMIN)
    user_service.update_user(superadmin, other, display_name="", role=Role.ADMIN)
    other.refresh_from_db()
    assert other.role == Role.ADMIN


# --- User management screens ---------------------------------------------------


def test_only_superadmin_manages_users(client, admin_user, superadmin):
    client.force_login(admin_user)
    assert client.get(reverse("user_list")).status_code == 403

    client.force_login(superadmin)
    assert client.get(reverse("user_list")).status_code == 200


def test_superadmin_creates_user_from_screen(client, superadmin):
    client.force_login(superadmin)

    response = client.post(
        reverse("user_create"),
        {"username": "tia.rosa", "display_name": "Tía Rosa", "role": Role.COLLABORATOR},
    )

    assert response.status_code == 302
    user = User.objects.get(username="tia.rosa")
    assert user.must_change_password
    assert AuditLog.objects.filter(action="user.create").exists()


def test_deactivate_last_superadmin_from_screen_shows_error(client, superadmin):
    client.force_login(superadmin)

    client.post(reverse("user_toggle_active", args=[superadmin.pk]))

    superadmin.refresh_from_db()
    assert superadmin.is_active


def test_user_search(client, superadmin, alice, bob):
    client.force_login(superadmin)
    response = client.get(reverse("user_list"), {"q": "alic"})
    assert b"alice" in response.content
    assert b"@bob" not in response.content


def test_profile_updates_display_name(client, alice):
    client.force_login(alice)
    client.post(reverse("profile"), {"display_name": "Ali", "save_profile": "1"})
    alice.refresh_from_db()
    assert alice.display_name == "Ali"


def test_link_user_to_person(client, superadmin, alice):
    from catalog.models import Person

    person = Person.objects.create(display_name="Alicia García")
    client.force_login(superadmin)
    client.post(
        reverse("user_edit", args=[alice.pk]),
        {"display_name": "Alicia", "role": Role.COLLABORATOR, "person": person.pk},
    )
    person.refresh_from_db()
    assert person.user == alice


# --- Login throttling ---------------------------------------------------------


def test_login_is_locked_after_five_failures(client, alice):
    for _ in range(5):
        login(client, "alice", "mala")

    response = login(client, "alice", "Contrasena-segura-1")

    assert response.status_code == 429
    assert "_auth_user_id" not in client.session
