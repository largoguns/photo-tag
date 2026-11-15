from django.shortcuts import render
from django.urls import reverse

from accounts.services.users import avatar_ids
from gamification.services import scoring

PERIODS = ("week", "month", "total")


def ranking(request):
    period = request.GET.get("period", "week")
    if period not in PERIODS:
        period = "week"
    rows = scoring.ranking(period)
    avatars = avatar_ids([user for user, _points, _level in rows])
    entries = [
        {
            "position": index + 1,
            "user": user,
            "points": points,
            "level": level,
            "avatar": avatars.get(user.pk),
            "me": user.pk == request.user.pk,
        }
        for index, (user, points, level) in enumerate(rows)
    ]
    return render(
        request,
        "gamification/ranking.html",
        {
            "period": period,
            "podium": [e for e in entries[:3] if e["points"] > 0],
            "entries": entries,
            "tab_urls": {p: f"{reverse('ranking')}?period={p}" for p in PERIODS},
        },
    )
