# farms/tests/test_admin_farm_privacy.py
"""
The Farm admin page shows identity only — name, active, created date —
plus a membership inline limited to name and role. No address, no owner
column, no operational/business figures (house counts, active batches,
birds alive), and no inline that shows records.
"""
import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from accounts.models import User
from checkngo.urls import ADMIN_URL

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
    # Not Client.login() — axes' auth backend requires a request, which
    # only a real POST to the login view supplies.
    client = Client()
    resp = client.post(
        reverse("admin:login"),
        {"username": superuser.phone_number, "password": PASSWORD},
    )
    assert resp.status_code == 302, resp.content
    return client


class TestFarmAdminShowsNoOperationalData:
    def test_changelist_has_no_address_or_owner_columns(
        self, admin_client, staffed_farm
    ):
        staffed_farm.address = "123 Rizal St"
        staffed_farm.municipality = "Batangas City"
        staffed_farm.province = "Batangas"
        staffed_farm.save()

        resp = admin_client.get(f"/{ADMIN_URL}farms/farm/")
        assert resp.status_code == 200
        body = resp.content.decode()
        assert staffed_farm.address not in body
        assert staffed_farm.province not in body
        assert staffed_farm.owner.full_name not in body

    def test_change_form_has_no_address_or_owner_fields(
        self, admin_client, staffed_farm
    ):
        resp = admin_client.get(
            f"/{ADMIN_URL}farms/farm/{staffed_farm.pk}/change/"
        )
        assert resp.status_code == 200
        body = resp.content.decode()
        assert 'name="address"' not in body
        assert 'name="owner"' not in body
        assert 'name="municipality"' not in body
        assert 'name="province"' not in body
        assert 'name="name"' in body
        assert 'name="is_active"' in body

    def test_membership_inline_shows_only_name_and_role(
        self, admin_client, staffed_farm, manager
    ):
        resp = admin_client.get(
            f"/{ADMIN_URL}farms/farm/{staffed_farm.pk}/change/"
        )
        body = resp.content.decode()
        assert manager.full_name in body
        # The inline's own field list is restricted to user + role; the
        # fuller FarmMembership form (joined_at, invited_by, is_active)
        # has its own dedicated page instead.
        assert "invited_by" not in body

    def test_add_farm_via_admin_is_refused(self, admin_client):
        """Farms are created through the owner's setup wizard, not here."""
        resp = admin_client.get(f"/{ADMIN_URL}farms/farm/add/")
        assert resp.status_code == 403
