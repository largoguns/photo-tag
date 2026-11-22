"""Readable audit: event catalogue, timeline of everything recorded, per-user summary.

Sources: AuditLog (actions done in the app) plus django-axes sign-ins (AccessLog) and failed
attempts (AccessFailureLog), shown as the virtual events "auth.login" and "auth.failed".
"""

from dataclasses import dataclass, field
from datetime import timedelta

from django.db.models import Count, Q
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from accounts.models import AuditLog, User

LOGIN, FAILED = "auth.login", "auth.failed"

CATEGORIES = {
    "catalog": (_("Catalogación"), "how_to_vote"),
    "photos": (_("Fotos"), "photo_library"),
    "scans": (_("Escaneos y subidas"), "cloud_upload"),
    "persons": (_("Personas"), "group"),
    "users": (_("Usuarios y accesos"), "manage_accounts"),
    "notifications": (_("Avisos"), "notifications"),
    "admin": (_("Administración"), "admin_panel_settings"),
}

# action: (category, label)
EVENTS = {
    "proposal.create": ("catalog", _("Propuesta creada")),
    "vote.cast": ("catalog", _("Voto")),
    "proposal.admin_accept": ("catalog", _("Propuesta aceptada por un administrador")),
    "proposal.admin_reject": ("catalog", _("Propuesta rechazada por un administrador")),
    "photo.rotate": ("photos", _("Foto girada")),
    "photo.crop": ("photos", _("Foto recortada")),
    "photo.delete": ("photos", _("Foto borrada")),
    "photo.discard": ("photos", _("Recorte descartado")),
    "photo.restore": ("photos", _("Recorte restaurado")),
    "photo.not_duplicate": ("photos", _("Marcada como no duplicada")),
    "photo.share": ("photos", _("Fotos compartidas")),
    "photo.download": ("photos", _("Fotos descargadas")),
    "upload.create": ("scans", _("Subida")),
    "scan.request": ("scans", _("Escaneo pedido")),
    "scan.reviewed": ("scans", _("Escaneo revisado")),
    "scan.reprocess": ("scans", _("Escaneo reprocesado")),
    "scan.delete": ("scans", _("Escaneo borrado")),
    "person.merge": ("persons", _("Personas fusionadas")),
    "person.avatar": ("persons", _("Foto principal cambiada")),
    "person.rename": ("persons", _("Persona renombrada")),
    "relationship.add": ("persons", _("Relación añadida")),
    "relationship.delete": ("persons", _("Relación quitada")),
    "person.honorific": ("persons", _("Honorífico cambiado")),
    "person.nickname_add": ("persons", _("Apodo añadido")),
    "person.nickname_remove": ("persons", _("Apodo quitado")),
    LOGIN: ("users", _("Inicio de sesión")),
    FAILED: ("users", _("Intento de acceso fallido")),
    "user.create": ("users", _("Usuario creado")),
    "user.update": ("users", _("Usuario modificado")),
    "user.activate": ("users", _("Usuario reactivado")),
    "user.deactivate": ("users", _("Usuario desactivado")),
    "user.reset_password": ("users", _("Contraseña restablecida")),
    "user.delete": ("users", _("Usuario borrado")),
    "user.link_person": ("users", _("Usuario vinculado a una persona")),
    "user.claim_person": ("users", _("Se identificó como una persona")),
    "user.unclaim_person": ("users", _("Dejó de identificarse como una persona")),
    "user.profile_photo": ("users", _("Foto de perfil cambiada")),
    "user.bootstrap_superadmin": ("users", _("Superadministrador inicial creado")),
    "push.subscribe": ("notifications", _("Avisos activados en un dispositivo")),
    "push.unsubscribe": ("notifications", _("Avisos desactivados en un dispositivo")),
    "event_type.create": ("admin", _("Tipo de evento creado")),
    "event_type.update": ("admin", _("Tipo de evento modificado")),
    "event_type.accept": ("admin", _("Evento nuevo aceptado")),
    "event_type.reject": ("admin", _("Evento nuevo rechazado")),
    "location.accept": ("admin", _("Lugar nuevo aceptado")),
    "location.reject": ("admin", _("Lugar nuevo rechazado")),
    "event_type.to_location": ("admin", _("Tipo de evento convertido en ubicación")),
    "event_type.to_other": ("admin", _("Tipo de evento convertido en «Otro»")),
    "event_type.merge": ("admin", _("Tipos de evento fusionados")),
    "event_type.keep": ("admin", _("Tipo de evento añadido a la lista estándar")),
    "location.rename": ("admin", _("Ubicación renombrada")),
    "location.update": ("admin", _("Ubicación activada o desactivada")),
    "location.merge": ("admin", _("Ubicaciones fusionadas")),
    "location.place": ("admin", _("Ubicación situada en el mapa")),
    "location.unplace": ("admin", _("Ubicación quitada del mapa")),
    "settings.thresholds": ("admin", _("Umbrales cambiados")),
    "export.request": ("admin", _("Exportación pedida")),
}

TARGET_NAMES = {
    "catalog.photo": _("Foto"),
    "catalog.proposal": _("Propuesta"),
    "catalog.person": _("Persona"),
    "catalog.relationship": _("Relación"),
    "catalog.scan": _("Escaneo"),
    "catalog.upload": _("Subida"),
    "catalog.eventtype": _("Tipo de evento"),
    "catalog.location": _("Ubicación"),
    "accounts.user": _("Usuario"),
}

PERIODS = {
    "today": (_("Hoy"), None),
    "7d": (_("Últimos 7 días"), 7),
    "30d": (_("Últimos 30 días"), 30),
    "year": (_("Este año"), None),
}


def event_choices():
    """[(category key, label, icon, [(action, label)])] for the event filter."""
    groups = []
    for key, (label, icon) in CATEGORIES.items():
        events = [(action, name) for action, (cat, name) in EVENTS.items() if cat == key]
        groups.append((key, label, icon, events))
    return groups


def event_label(value):
    """Label of a filter value: an action or "cat:<category>"."""
    if value.startswith("cat:"):
        category = CATEGORIES.get(value[4:])
        return category[0] if category else None
    event = EVENTS.get(value)
    return event[1] if event else None


def _actions_for(value):
    if not value:
        return None
    if value.startswith("cat:"):
        return {a for a, (cat, _label) in EVENTS.items() if cat == value[4:]} or {"-"}
    return {value}


def period_start(period, now=None):
    now = timezone.localtime(now or timezone.now())
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "today":
        return start_of_day
    if period == "year":
        return start_of_day.replace(month=1, day=1)
    days = PERIODS.get(period, (None, None))[1]
    return now - timedelta(days=days) if days else None


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------


def _axes_models():
    from axes.models import AccessFailureLog, AccessLog

    return AccessLog, AccessFailureLog


def timeline_keys(user=None, event="", period=""):
    """[(time, source, id)] newest first, for every matching record (light tuples)."""
    actions = _actions_for(event)
    since = period_start(period)
    keys = []
    audit = AuditLog.objects.all()
    if user is not None:
        audit = audit.filter(user=user)
    if actions is not None:
        audit = audit.filter(action__in=actions)
    if since is not None:
        audit = audit.filter(created_at__gte=since)
    keys += [(t, "audit", pk) for t, pk in audit.values_list("created_at", "pk")]
    access_log, failure_log = _axes_models()
    for source, model, action in (("login", access_log, LOGIN), ("failed", failure_log, FAILED)):
        if actions is not None and action not in actions:
            continue
        rows = model.objects.all()
        if user is not None:
            rows = rows.filter(username=user.username)
        if since is not None:
            rows = rows.filter(attempt_time__gte=since)
        keys += [(t, source, pk) for t, pk in rows.values_list("attempt_time", "pk")]
    keys.sort(key=lambda key: key[0], reverse=True)
    return keys


@dataclass
class Entry:
    time: object
    action: str
    label: str
    category: str
    icon: str
    user: object = None  # User, or a user name for sign-ins of unknown users
    target: str = ""
    url: str = ""
    details: list = field(default_factory=list)


def _target_text_and_url(entry, targets):
    obj = targets.get((entry.target_type, entry.target_id))
    kind = entry.target_type
    if obj is None:
        if not kind:
            return "", ""
        # Gone since: name it from what was recorded with the action.
        details = entry.details or {}
        name = next(
            (str(details[k]) for k in ("file", "name", "username", "label") if details.get(k)),
            None,
        )
        if name is None:
            name = str(TARGET_NAMES.get(kind, kind)) + f" {entry.target_id}"
        return str(_("%(name)s (ya no existe)") % {"name": name}), ""
    try:
        if kind == "catalog.photo":
            return str(_("Foto %(id)s") % {"id": obj.pk}), reverse("photo_detail", args=[obj.pk])
        if kind == "catalog.proposal":
            text = _("«%(label)s» en la foto %(photo)s") % {
                "label": obj.label(),
                "photo": obj.photo_id,
            }
            return str(text), reverse("photo_detail", args=[obj.photo_id])
        if kind == "catalog.relationship":
            return str(obj), reverse("person_detail", args=[obj.person_from_id])
        if kind == "catalog.person":
            return obj.shown_name, reverse("person_detail", args=[obj.pk])
        if kind == "accounts.user":
            return f"@{obj.username}", reverse("user_edit", args=[obj.pk])
        if kind == "catalog.scan":
            return obj.file_name, ""
        if kind == "catalog.upload":
            return obj.original_name, ""
        if kind == "catalog.location":
            return obj.name, reverse("admin_location_edit", args=[obj.pk])
        if kind == "catalog.eventtype":
            return str(obj), reverse("admin_event_type_edit", args=[obj.pk])
    except NoReverseMatch:
        pass
    return str(obj), ""


def _load_targets(entries):
    """{(target_type, target_id): object} for the audit entries of one page."""
    from django.apps import apps

    wanted = {}
    for entry in entries:
        if entry.target_type and entry.target_id.isdigit():
            wanted.setdefault(entry.target_type, set()).add(int(entry.target_id))
    targets = {}
    for label, ids in wanted.items():
        try:
            model = apps.get_model(label)
        except LookupError:
            continue
        for pk, obj in model.objects.in_bulk(ids).items():
            targets[(label, str(pk))] = obj
    return targets


def _details(values):
    rows = []
    for key, value in (values or {}).items():
        if isinstance(value, list) and len(value) > 12:
            value = ", ".join(map(str, value[:12])) + " …"
        rows.append((key, value))
    return rows


def _audit_entry(entry, targets):
    category, label = EVENTS.get(entry.action, ("admin", entry.action))
    target, url = _target_text_and_url(entry, targets)
    return Entry(
        time=entry.created_at,
        action=entry.action,
        label=label,
        category=category,
        icon=CATEGORIES[category][1],
        user=entry.user,
        target=target,
        url=url,
        details=_details(entry.details),
    )


def _access_entry(row, action, users):
    category, label = EVENTS[action]
    details = [("ip", row.ip_address or "—"), ("device", row.user_agent[:120])]
    if action == LOGIN and row.logout_time:
        details.append(("logout", timezone.localtime(row.logout_time).strftime("%Y-%m-%d %H:%M")))
    if action == FAILED and getattr(row, "locked_out", False):
        details.append(("locked_out", True))
    return Entry(
        time=row.attempt_time,
        action=action,
        label=label,
        category=category,
        icon=CATEGORIES[category][1],
        user=users.get(row.username, row.username),
        details=details,
    )


def build_entries(keys):
    """Entries for a page of timeline keys, loading only those records."""
    access_log, failure_log = _axes_models()
    ids = {"audit": [], "login": [], "failed": []}
    for _time, source, pk in keys:
        ids[source].append(pk)
    audit = AuditLog.objects.select_related("user").in_bulk(ids["audit"])
    logins = access_log.objects.in_bulk(ids["login"])
    failures = failure_log.objects.in_bulk(ids["failed"])
    names = {r.username for r in [*logins.values(), *failures.values()] if r.username}
    users = {u.username: u for u in User.objects.filter(username__in=names)}
    targets = _load_targets(audit.values())
    entries = []
    for _time, source, pk in keys:
        if source == "audit" and pk in audit:
            entries.append(_audit_entry(audit[pk], targets))
        elif source == "login" and pk in logins:
            entries.append(_access_entry(logins[pk], LOGIN, users))
        elif source == "failed" and pk in failures:
            entries.append(_access_entry(failures[pk], FAILED, users))
    return entries


def recent(limit=15):
    """Latest recorded actions, readable (administration home)."""
    keys = [(t, "audit", pk) for t, pk in AuditLog.objects.values_list("created_at", "pk")[:limit]]
    return build_entries(keys)


# ---------------------------------------------------------------------------
# Per-user summary
# ---------------------------------------------------------------------------


def summary(user):
    from catalog.models import Proposal, Upload, Vote
    from scanner.models import ScanJob

    access_log, _failure_log = _axes_models()
    proposals = Proposal.objects.filter(author=user).aggregate(
        total=Count("pk"),
        open=Count("pk", filter=Q(status=Proposal.Status.OPEN)),
        accepted=Count("pk", filter=Q(status=Proposal.Status.ACCEPTED)),
        rejected=Count("pk", filter=Q(status=Proposal.Status.REJECTED)),
    )
    votes = Vote.objects.filter(user=user).aggregate(
        up=Count("pk", filter=Q(value=Vote.UP)), down=Count("pk", filter=Q(value=Vote.DOWN))
    )
    return {
        "proposals": proposals,
        "votes": votes,
        "uploads": Upload.objects.filter(user=user).count(),
        "scans": ScanJob.objects.filter(requested_by=user).count(),
        "actions": AuditLog.objects.filter(user=user).count(),
        "logins": access_log.objects.filter(username=user.username).count(),
    }
