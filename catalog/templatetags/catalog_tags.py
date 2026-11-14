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
