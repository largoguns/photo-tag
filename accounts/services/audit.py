from accounts.models import AuditLog


def log(user, action, target=None, **details):
    """Record an auditable action. `target` is any model instance (or None)."""
    target_type = target_id = ""
    if target is not None:
        target_type = target._meta.label_lower
        target_id = str(target.pk)
    return AuditLog.objects.create(
        user=user if user is not None and user.is_authenticated else None,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=details,
    )
