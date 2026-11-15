from django.contrib.auth.models import AbstractUser, UserManager
from django.db import models
from django.db.models import ProtectedError
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _


class Role(models.TextChoices):
    SUPERADMIN = "superadmin", "Superadmin"
    ADMIN = "admin", _("Administrador")
    COLLABORATOR = "collaborator", _("Colaborador")


class Theme(models.TextChoices):
    AUTO = "auto", _("Automático (según el sistema)")
    LIGHT = "light", _("Claro")
    DARK = "dark", _("Oscuro")


class User(AbstractUser):
    display_name = models.CharField(_("nombre a mostrar"), max_length=150, blank=True)
    role = models.CharField(
        _("rol"), max_length=20, choices=Role.choices, default=Role.COLLABORATOR
    )
    must_change_password = models.BooleanField(_("debe cambiar la contraseña"), default=True)
    theme = models.CharField(_("tema"), max_length=10, choices=Theme.choices, default=Theme.AUTO)
    # Profile photo chosen among the faces of the person the user identified as.
    avatar_face = models.ForeignKey(
        "catalog.Face", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    objects = UserManager()

    class Meta:
        ordering = ["username"]

    def __str__(self):
        return self.get_display_name()

    def get_display_name(self):
        return self.display_name or self.username

    @property
    def is_player(self):
        """Only collaborators take part in points, levels and the ranking."""
        return self.role == Role.COLLABORATOR

    @property
    def is_superadmin(self):
        return self.role == Role.SUPERADMIN

    @property
    def is_catalog_admin(self):
        """Administrators and superadmins share the catalog administration permissions."""
        return self.role in (Role.ADMIN, Role.SUPERADMIN)


class AuditLog(models.Model):
    user = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_entries"
    )
    action = models.CharField(max_length=64, db_index=True)
    target_type = models.CharField(max_length=64, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.user} {self.action}"


@receiver(pre_delete, sender=User)
def protect_last_superadmin(sender, instance, **kwargs):
    if instance.role == Role.SUPERADMIN and instance.is_active:
        others = User.objects.filter(role=Role.SUPERADMIN, is_active=True).exclude(pk=instance.pk)
        if not others.exists():
            raise ProtectedError(_("Debe existir al menos un superadmin activo."), {instance})
