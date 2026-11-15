from django import template
from django.utils.html import format_html
from django.utils.translation import gettext as _

from gamification.services.levels import LEVELS

register = template.Library()


@register.simple_tag
def level_ring(level):
    """Attributes of the belt ring: the current level's colour, filling with the next one."""
    number = level.number
    following = min(number + 1, len(LEVELS))
    percent = level.percent if number < len(LEVELS) else 0
    title = _("Nivel %(number)s · %(name)s · %(percent)s %%") % {
        "number": number,
        "name": level.name,
        "percent": level.percent,
    }
    return format_html(
        'class="level-ring belt-{}" style="--p: {}; --current: var(--belt-{}); '
        '--next: var(--belt-{})" title="{}"',
        number,
        percent,
        number,
        following,
        title,
    )
