from django import template

register = template.Library()


@register.filter
def thumb_width(photo, size):
    """Actual width of the width-bounded thumbnail, for srcset descriptors."""
    return min(int(size), photo.width)


@register.filter
def css_num(value):
    """Locale-independent number for inline CSS (Spanish locale would use a comma)."""
    return f"{float(value):.3f}"


@register.simple_tag
def icon(name, label=""):
    """Inline reference to a Material Symbols icon in static/img/icons.svg."""
    from django.templatetags.static import static
    from django.utils.html import format_html

    if label:
        return format_html(
            '<svg class="icon" role="img" aria-label="{}"><use href="{}#i-{}"></use></svg>',
            label,
            static("img/icons.svg"),
            name,
        )
    return format_html(
        '<svg class="icon" aria-hidden="true" focusable="false"><use href="{}#i-{}"></use></svg>',
        static("img/icons.svg"),
        name,
    )


@register.simple_tag
def qs(params, key, value=None):
    """Query string with `key` set to `value` (or removed when None); paging is reset."""
    query = params.copy()
    for name in ("page", "last_group"):
        query.pop(name, None)
    if value is None or value == "":
        query.pop(key, None)
    else:
        query[key] = value
    encoded = query.urlencode()
    return f"?{encoded}" if encoded else "?"


@register.simple_tag
def sheet_options(items, kind="choices"):
    """Normalize filter options to (value, label, avatar_face_id) tuples."""
    if kind == "person":
        return [(p.pk, p.display_name, getattr(p, "avatar_id", None)) for p in items]
    if kind == "event":
        return [(e.pk, str(e), None) for e in items]
    if kind == "decade":
        return [(d, f"{d}s", None) for d in items]
    return [(value, label, None) for value, label in items]


@register.filter
def aspect_ratio(photo):
    """Width/height as a locale-independent number for inline CSS."""
    return f"{photo.width / photo.height:.4f}" if photo.height else "1"


@register.filter
def rotation_fit(photo):
    """Scale that keeps a photo inside its frame while previewing a 90/270° rotation."""
    if photo.pending_rotation % 180 == 0 or not photo.width or not photo.height:
        return "1"
    ratio = photo.width / photo.height
    return f"{min(ratio, 1 / ratio):.4f}"
