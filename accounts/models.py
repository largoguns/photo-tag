from django.contrib.auth.models import AbstractUser, UserManager
from django.db import models
from django.db.models import ProtectedError
from django.db.models.signals import pre_delete
from django.dispatch import receiver


class Role(models.TextChoices):
    SUPERADMIN = "superadmin", "Superadmin"
    ADMIN = "admin", "Administrador"
    COLLABORATOR = "collaborator", "Colaborador"


class User(AbstractUser):
    display_name = models.CharField("nombre a mostrar", max_length=150, blank=True)
    role = models.CharField("rol", max_length=20, choices=Role.choices, default=Role.COLLABORATOR)
    must_change_password = models.BooleanField("debe cambiar la contraseña", default=True)

    objects = UserManager()

    class Meta:
        ordering = ["username"]

    def __str__(self):
        return self.get_display_name()

    def get_display_name(self):
        return self.display_name or self.username

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
            raise ProtectedError("Debe existir al menos un superadmin activo.", {instance})
