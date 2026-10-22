# farms/evaluation_admin.py
"""
System → Evaluation farms: the admin page that creates and archives the
farms used in the guided test sessions (see production/evaluation.py).

Superusers only — anyone else signed in gets a 403. The URLs hang off
FarmAdmin (farms/admin.py) so they sit inside the admin site, behind its
login, CSRF and never-cache wrappers.

What the pages show is deliberately thin: farm names, active flag and
created date in the list; the four demo accounts (role, name, phone, PIN)
once, on the response to the create request, and never again. Nothing
about the farm's houses, flocks, sales or stock.
"""
import secrets
from functools import wraps

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path
from django.views.decorators.http import require_POST

from production.evaluation import (
    EVALUATION_FARM_PREFIX,
    MAX_FARMS_PER_REQUEST,
    EvaluationFarmError,
    archive_evaluation_farms,
    create_evaluation_farms,
    evaluation_farms,
)

from .models import Farm

# One token per rendering of the create form, kept in the admin's session
# and spent by the first POST that carries it — so reloading the result
# page (which would resubmit the form) cannot create a second set of farms.
TOKEN_SESSION_KEY = "evaluation_farms_create_token"


class CreateEvaluationFarmsForm(forms.Form):
    count = forms.IntegerField(min_value=1, max_value=MAX_FARMS_PER_REQUEST)
    token = forms.CharField()


def evaluation_urls(model_admin):
    site = model_admin.admin_site

    def superuser_only(view):
        """
        Checked before the admin's own wrapper so a signed-in non-superuser
        gets a 403 rather than a bounce to the login page. Anonymous visitors
        still go to the login page.
        """

        @wraps(view)
        def wrapped(request, *args, **kwargs):
            user = request.user
            if user.is_authenticated and not (user.is_active and user.is_superuser):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapped

    def bind(view):
        return superuser_only(site.admin_view(lambda request: view(request, model_admin)))

    return [
        path("evaluation/", bind(index_view), name="farms_farm_evaluation"),
        path("evaluation/create/", bind(create_view), name="farms_farm_evaluation_create"),
        path("evaluation/archive/", bind(archive_view), name="farms_farm_evaluation_archive"),
    ]


def _context(request, model_admin, **extra):
    return {
        **model_admin.admin_site.each_context(request),
        "title": "Evaluation farms",
        # Unfold's page header reads content_title when there is no model.
        "content_title": "Evaluation farms",
        "max_count": MAX_FARMS_PER_REQUEST,
        **extra,
    }


def index_view(request, model_admin):
    token = secrets.token_urlsafe(16)
    request.session[TOKEN_SESSION_KEY] = token
    return TemplateResponse(
        request,
        "admin/evaluation_farms/index.html",
        _context(request, model_admin, farms=evaluation_farms(), token=token),
    )


@require_POST
def create_view(request, model_admin):
    form = CreateEvaluationFarmsForm(request.POST)
    if not form.is_valid():
        messages.error(
            request, f"Choose between 1 and {MAX_FARMS_PER_REQUEST} farms. Nothing was created."
        )
        return redirect("admin:farms_farm_evaluation")

    expected = request.session.pop(TOKEN_SESSION_KEY, None)
    request.session.save()
    if not expected or not secrets.compare_digest(expected, form.cleaned_data["token"]):
        messages.warning(
            request,
            "That form was already submitted, so nothing more was created. "
            "Use the form below to create more farms.",
        )
        return redirect("admin:farms_farm_evaluation")

    try:
        results = create_evaluation_farms(
            form.cleaned_data["count"], created_by=request.user
        )
    except EvaluationFarmError as exc:
        messages.error(request, str(exc))
        return redirect("admin:farms_farm_evaluation")

    # Rendered on this response, never stored: the admin's never-cache
    # wrapper keeps the browser from caching the PINs either.
    return TemplateResponse(
        request,
        "admin/evaluation_farms/created.html",
        _context(request, model_admin, title="Evaluation farms created", results=results),
    )


@require_POST
def archive_view(request, model_admin):
    ids = [value for value in request.POST.getlist("farm_ids") if value.isdigit()]
    farms = Farm.objects.filter(
        pk__in=ids, name__startswith=EVALUATION_FARM_PREFIX
    ).select_related("owner")
    if not farms:
        messages.warning(request, "Select at least one evaluation farm to archive.")
        return redirect("admin:farms_farm_evaluation")

    result = archive_evaluation_farms(list(farms), archived_by=request.user)
    messages.success(
        request,
        f"Archived {len(result.archived)} evaluation farm(s) and deactivated "
        f"{result.accounts_deactivated} demo account(s). Nothing was deleted.",
    )
    if result.skipped:
        messages.warning(
            request,
            f"Left {len(result.skipped)} farm(s) untouched: not evaluation farms.",
        )
    return redirect("admin:farms_farm_evaluation")
