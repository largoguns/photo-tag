"""User management rules (spec section 4). Views must go through these functions."""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import transaction
from django.utils.translation import gettext as _

from accounts.models import Role, User
from accounts.services import audit


class LastSuperadminError(ValidationError):
    pass


def generic_password():
    password = settings.DEFAULT_USER_PASSWORD
    if not password:
        raise ImproperlyConfigured("DEFAULT_USER_PASSWORD is not configured")
    return password


def active_superadmins():
    return User.objects.filter(role=Role.SUPERADMIN, is_active=True)


def _guard_last_superadmin(user):
    """Raise if `user` is the only active superadmin left."""
    if user.role == Role.SUPERADMIN and user.is_active:
        if not active_superadmins().exclude(pk=user.pk).exists():
            raise LastSuperadminError(_("Debe existir al menos un superadmin activo."))


@transaction.atomic
def create_user(actor, *, username, display_name="", role=Role.COLLABORATOR, password=None):
    user = User(username=username, display_name=display_name, role=role)
    user.set_password(password or generic_password())
    user.must_change_password = True
    user.full_clean(exclude=["password"])
    user.save()
    audit.log(actor, "user.create", user, username=username, role=role)
    return user


@transaction.atomic
def update_user(actor, user, *, display_name, role):
    user = User.objects.select_for_update().get(pk=user.pk)
    if role != user.role:
        _guard_last_superadmin(user)
    changes = {}
    if display_name != user.display_name:
        changes["display_name"] = [user.display_name, display_name]
    if role != user.role:
        changes["role"] = [user.role, role]
    user.display_name = display_name
    user.role = role
    user.save(update_fields=["display_name", "role"])
    if changes:
        audit.log(actor, "user.update", user, **changes)
    return user


@transaction.atomic
def set_active(actor, user, active):
    user = User.objects.select_for_update().get(pk=user.pk)
    if not active:
        _guard_last_superadmin(user)
    user.is_active = active
    user.save(update_fields=["is_active"])
    audit.log(actor, "user.activate" if active else "user.deactivate", user)
    return user


@transaction.atomic
def reset_password(actor, user):
    user.set_password(generic_password())
    user.must_change_password = True
    user.save(update_fields=["password", "must_change_password"])
    audit.log(actor, "user.reset_password", user)
    return user


@transaction.atomic
def delete_user(actor, user):
    """Users are normally deactivated; deletion is only allowed if it keeps a superadmin."""
    _guard_last_superadmin(user)
    audit.log(actor, "user.delete", user, username=user.username)
    user.delete()


def change_own_password(user, new_password):
    user.set_password(new_password)
    user.must_change_password = False
    user.save(update_fields=["password", "must_change_password"])


@transaction.atomic
def link_person(actor, user, person):
    from catalog.models import Person

    Person.objects.filter(user=user).exclude(pk=getattr(person, "pk", None)).update(user=None)
    if person is not None:
        person.user = user
        person.save(update_fields=["user"])
    audit.log(actor, "user.link_person", user, person_id=getattr(person, "pk", None))


def ensure_superadmin():
    """Create the initial superadmin from the environment if none exists. Returns it or None."""
    if User.objects.filter(role=Role.SUPERADMIN).exists():
        return None
    password = settings.SUPERADMIN_PASSWORD
    if not password:
        raise ImproperlyConfigured("SUPERADMIN_PASSWORD is required to create the first superadmin")
    username = settings.SUPERADMIN_USERNAME
    user, _ = User.objects.get_or_create(username=username)
    user.role = Role.SUPERADMIN
    user.is_active = True
    user.must_change_password = False
    user.set_password(password)
    user.save()
    audit.log(None, "user.bootstrap_superadmin", user, username=username)
    return user
