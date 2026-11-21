"""Person sheet actions: honorific, nicknames and relationships (spec 5.12 and 5.13).

Saved directly, without a vote. Plain POST forms that go back to the person sheet
(boosted with HTMX in the page, so only the main content is swapped); errors are shown
as messages.
"""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from catalog.models import Person, PersonNickname, Relationship
from catalog.services import person_details, relationships


def _back(request, person_id, sheet=""):
    """Back to the person page, opening again the sheet the form came from."""
    url = request.POST.get("next")
    if not url or not url_has_allowed_host_and_scheme(
        url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        url = reverse("person_detail", args=[person_id])
    return redirect(f"{url}?sheet={sheet}" if sheet else url)


def _run(request, person_id, sheet, action, success=None):
    try:
        action()
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        if success:
            messages.success(request, success)
    return _back(request, person_id, sheet)


@require_POST
def set_honorific(request, pk):
    person = get_object_or_404(Person, pk=pk)
    return _run(
        request,
        person.pk,
        "",
        lambda: person_details.set_honorific(
            request.user, person, request.POST.get("honorific", "")
        ),
    )


@require_POST
def add_nickname(request, pk):
    person = get_object_or_404(Person, pk=pk)
    return _run(
        request,
        person.pk,
        "nicknames",
        lambda: person_details.add_nickname(request.user, person, request.POST.get("nickname", "")),
    )


@require_POST
def remove_nickname(request, pk):
    nickname = get_object_or_404(PersonNickname, pk=pk)
    return _run(
        request,
        nickname.person_id,
        "nicknames",
        lambda: person_details.remove_nickname(request.user, nickname),
    )


@require_POST
def add_relationship(request, pk):
    person = get_object_or_404(Person, pk=pk)

    def action():
        other_id = request.POST.get("person_id", "")
        other = (
            get_object_or_404(Person.objects.active(), pk=other_id) if other_id.isdigit() else None
        )
        relationships.add(
            request.user,
            person,
            request.POST.get("role", ""),
            other=other,
            other_name=request.POST.get("name", ""),
            new_person=request.POST.get("new_person") == "1",
            activity=request.POST.get("activity", ""),
        )

    return _run(request, person.pk, "family", action, _("Relación guardada."))


@require_POST
def remove_relationship(request, pk):
    relationship = get_object_or_404(Relationship, pk=pk)
    return _run(
        request,
        relationship.person_from_id,
        "family",
        lambda: relationships.remove(request.user, relationship),
    )
