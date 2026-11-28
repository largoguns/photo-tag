from django.conf import settings

from accounts.services.users import avatar_face_id


def navigation(request):
    """Values the app shell (top bar, avatar, push prompt) needs on every page."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"nav_shell": False}
    avatar_face = avatar_face_id(user)
    level = None
    if user.is_player:
        from gamification.services.levels import level_for
        from gamification.services.scoring import total

        level = level_for(total(user))
    push_key = settings.VAPID_PUBLIC_KEY if settings.VAPID_PRIVATE_KEY else ""
    from catalog.services import questions

    return {
        # Questions waiting for this user (avatar menu, spec 5.17).
        "nav_questions": questions.pending_count(user),
        # Navigation is hidden until the generic password has been replaced.
        "nav_shell": not user.must_change_password,
        "nav_avatar_face_id": avatar_face,
        "nav_level": level,  # belt ring around the avatar (collaborators only)
        "nav_initial": (user.get_display_name()[:1] or "?").upper(),
        "nav_push_key": push_key,
        "nav_scanner": bool(settings.SCANNER_URL),
    }
