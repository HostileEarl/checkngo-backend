# partners/tests/test_admin_readonly.py
"""FarmPartnerLink's admin page is read-only — links are managed via the API."""
import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from accounts.models import User
from checkngo.urls import ADMIN_URL
from partners.models import FarmPartnerLink

pytestmark = pytest.mark.django_db

PASSWORD = "Correct-Horse-9-Battery"


@pytest.fixture(autouse=True)
def _static_storage_for_admin_pages(settings):
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    }
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def superuser(db):
    return User.objects.create_superuser(
        phone_number="+639170007000",
        password=PASSWORD,
        full_name="Platform Admin",
    )


@pytest.fixture
def admin_client(superuser):
    client = Client()
    resp = client.post(
        reverse("admin:login"),
        {"username": superuser.phone_number, "password": PASSWORD},
    )
    assert resp.status_code == 302, resp.content
    return client


@pytest.fixture
def link(db, staffed_farm, consumer, owner):
    return FarmPartnerLink.objects.create(
        farm=staffed_farm,
        partner=consumer,
        link_type=FarmPartnerLink.LinkType.CONSUMER,
        linked_by=owner,
    )


class TestFarmPartnerLinkAdminIsReadOnly:
    def test_changelist_loads(self, admin_client, link):
        resp = admin_client.get(f"/{ADMIN_URL}partners/farmpartnerlink/")
        assert resp.status_code == 200

    def test_add_is_refused(self, admin_client):
        assert (
            admin_client.get(f"/{ADMIN_URL}partners/farmpartnerlink/add/").status_code
            == 403
        )

    def test_change_page_is_view_only_no_save_button(self, admin_client, link):
        """
        has_change_permission=False with has_view_permission still True
        (Django's default) renders the detail page read-only rather than
        403ing — the save/delete actions below are the real guarantee.
        """
        resp = admin_client.get(
            f"/{ADMIN_URL}partners/farmpartnerlink/{link.pk}/change/"
        )
        assert resp.status_code == 200
        assert b'name="_save"' not in resp.content

    def test_posting_a_change_is_refused(self, admin_client, link):
        resp = admin_client.post(
            f"/{ADMIN_URL}partners/farmpartnerlink/{link.pk}/change/",
            {"notes": "tampered"},
        )
        assert resp.status_code == 403
        link.refresh_from_db()
        assert link.notes != "tampered"

    def test_delete_is_refused(self, admin_client, link):
        resp = admin_client.get(
            f"/{ADMIN_URL}partners/farmpartnerlink/{link.pk}/delete/"
        )
        assert resp.status_code == 403
