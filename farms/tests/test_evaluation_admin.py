# farms/tests/test_evaluation_admin.py
"""
System → Evaluation farms (farms/evaluation_admin.py).

- superusers only: a signed-in staff member or farm user gets a 403 on
  every URL and nothing is created or archived; anonymous visitors go to
  the login page
- the list shows evaluation farm names, active flag and created date —
  no other farm, no phone number, no farm data
- "Create farms" makes 1-10 farms on the next free session numbers and
  shows each farm's four accounts with their PINs on that response only,
  never-cache; resubmitting the same form creates nothing more
- "Archive selected evaluation farms" deactivates only evaluation farms and
  their accounts, and deletes nothing
- the sidebar link sits in the System hub, for superusers only
- no farm-data model is registered in the admin
"""
import re

import pytest
from django.contrib import admin
from django.contrib.admin.models import ADDITION, CHANGE, LogEntry
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from accounts.models import Invitation, User
from farms.models import Farm
from notifications.models import SmsLog
from production.evaluation import create_evaluation_farm, demo_phone_numbers
from production.models import Batch, DailyRecord, SaleEvent

pytestmark = pytest.mark.django_db

PASSWORD = "Correct-Horse-9-Battery"


@pytest.fixture(autouse=True)
def _admin_pages_render(settings):
    # Same as accounts/tests/test_admin_privacy.py: plain static storage, so
    # admin pages render without a collectstatic manifest under DEBUG=False.
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    cache.clear()
    yield
    cache.clear()


def _admin_login(phone):
    # A real POST, not Client.login(): axes' backend needs the request.
    client = Client()
    response = client.post(reverse("admin:login"), {"username": phone, "password": PASSWORD})
    assert response.status_code == 302, response.content
    return client


@pytest.fixture
def superuser(db):
    return User.objects.create_superuser(
        phone_number="+639170007000", password=PASSWORD, full_name="Platform Admin"
    )


@pytest.fixture
def superuser_client(superuser):
    return _admin_login(superuser.phone_number)


@pytest.fixture
def staff_client(db):
    User.objects.create_user(
        "+639170007001", PASSWORD, full_name="Staff Only", role=User.Role.OWNER,
        is_staff=True, must_change_credential=False,
    )
    return _admin_login("+639170007001")


@pytest.fixture
def farm_user_client(owner):
    client = Client()
    client.force_login(owner, backend="django.contrib.auth.backends.ModelBackend")
    return client


INDEX = "admin:farms_farm_evaluation"
CREATE = "admin:farms_farm_evaluation_create"
ARCHIVE = "admin:farms_farm_evaluation_archive"


def _token(client):
    body = client.get(reverse(INDEX)).content.decode()
    return re.search(r'name="token" value="([^"]+)"', body).group(1)


def _created_accounts(body):
    """(farm name, [(role, name, phone, pin), ...]) from the result page."""
    farms = re.findall(r"<h2>(Demo Farm – Session \d\d)</h2>", body)
    rows = re.findall(
        r"<td>(Owner|Manager|Worker|Buyer)</td>\s*<td>([^<]+)</td>\s*"
        r'<td class="checkngo-mono">(\+\d+)</td>\s*<td class="checkngo-mono checkngo-pin">(\d{6})</td>',
        body,
    )
    return farms, rows


class TestSuperusersOnly:
    @pytest.mark.parametrize("client_fixture", ["staff_client", "farm_user_client"])
    def test_everyone_else_gets_403_and_nothing_happens(self, request, client_fixture, owner):
        client = request.getfixturevalue(client_fixture)
        existing = create_evaluation_farm(1)

        assert client.get(reverse(INDEX)).status_code == 403
        assert client.post(reverse(CREATE), {"count": 1, "token": "x"}).status_code == 403
        response = client.post(reverse(ARCHIVE), {"farm_ids": [existing.farm.pk]})
        assert response.status_code == 403

        assert Farm.objects.filter(name__startswith="Demo Farm").count() == 1
        existing.farm.refresh_from_db()
        assert existing.farm.is_active

    def test_anonymous_goes_to_the_login_page(self, client):
        for name in (INDEX, CREATE, ARCHIVE):
            response = client.get(reverse(name))
            assert response.status_code == 302
            assert reverse("admin:login") in response["Location"]

    def test_superuser_sees_the_page(self, superuser_client):
        response = superuser_client.get(reverse(INDEX))
        assert response.status_code == 200
        body = response.content.decode()
        assert "Create farms" in body
        assert "No evaluation farms yet." in body

    def test_admin_url_path(self):
        assert reverse(INDEX).endswith("farms/farm/evaluation/")


class TestList:
    def test_shows_only_evaluation_farm_names_active_and_created(self, superuser_client, staffed_farm):
        result = create_evaluation_farm(2)
        body = superuser_client.get(reverse(INDEX)).content.decode()

        assert "Demo Farm – Session 02" in body
        assert result.farm.created_at.strftime("%Y") in body
        assert "Active" in body
        # Not a real farm, not a phone number, not farm data.
        assert staffed_farm.name not in body
        for phone in demo_phone_numbers(2):
            assert phone not in body
        for batch in Batch.objects.filter(house__farm=result.farm):
            assert batch.batch_code not in body
        assert "House 1" not in body

    def test_lookalike_farm_is_not_listed(self, superuser_client, owner):
        Farm.objects.create(name="Demo Farm – Session 50", owner=owner)
        body = superuser_client.get(reverse(INDEX)).content.decode()
        assert "Demo Farm – Session 50" not in body


class TestCreate:
    def test_creates_on_the_next_free_numbers_and_shows_pins_once(self, superuser_client, superuser):
        create_evaluation_farm(1)
        response = superuser_client.post(
            reverse(CREATE), {"count": 2, "token": _token(superuser_client)}
        )
        assert response.status_code == 200
        assert "no-store" in response["Cache-Control"]

        body = response.content.decode()
        farms, rows = _created_accounts(body)
        assert farms == ["Demo Farm – Session 02", "Demo Farm – Session 03"]
        assert len(rows) == 8
        assert "private session sheet" in body
        for role, name, phone, pin in rows:
            user = User.objects.get(phone_number=phone)
            assert user.full_name == name
            assert user.check_password(pin)
            assert user.must_change_credential is False
        # Nothing else about the farm.
        for batch in Batch.objects.filter(house__farm__name__in=farms):
            assert batch.batch_code not in body
        assert "House 1" not in body

        # The PINs are never shown again.
        listing = superuser_client.get(reverse(INDEX)).content.decode()
        for *_, pin in rows:
            assert f">{pin}<" not in listing

        logged = LogEntry.objects.filter(user=superuser, action_flag=ADDITION)
        assert sorted(entry.object_repr for entry in logged) == farms

        assert SmsLog.objects.count() == 0
        assert Invitation.objects.count() == 0

    def test_resubmitting_the_same_form_creates_nothing_more(self, superuser_client):
        data = {"count": 1, "token": _token(superuser_client)}
        assert superuser_client.post(reverse(CREATE), data).status_code == 200
        again = superuser_client.post(reverse(CREATE), data)
        assert again.status_code == 302
        assert Farm.objects.filter(name__startswith="Demo Farm").count() == 1

    @pytest.mark.parametrize("count", [0, 11, "", "two"])
    def test_count_outside_one_to_ten_creates_nothing(self, superuser_client, count):
        response = superuser_client.post(
            reverse(CREATE), {"count": count, "token": _token(superuser_client)}, follow=True
        )
        assert response.status_code == 200
        assert "Choose between 1 and 10" in response.content.decode()
        assert Farm.objects.count() == 0
        assert User.objects.filter(phone_number__startswith="+63999").count() == 0

    def test_a_taken_number_is_skipped_not_reused(self, superuser_client):
        User.objects.create_user(
            "+639990100003", "135790", full_name="Real Person", role=User.Role.WORKER
        )
        response = superuser_client.post(
            reverse(CREATE), {"count": 1, "token": _token(superuser_client)}
        )
        farms, _ = _created_accounts(response.content.decode())
        assert farms == ["Demo Farm – Session 02"]
        real = User.objects.get(phone_number="+639990100003")
        assert real.full_name == "Real Person" and real.check_password("135790")

    def test_get_on_create_does_nothing(self, superuser_client):
        assert superuser_client.get(reverse(CREATE)).status_code == 405
        assert Farm.objects.count() == 0


class TestArchive:
    def test_archives_only_evaluation_farms_and_deletes_nothing(
        self, superuser_client, superuser, staffed_farm, owner
    ):
        first = create_evaluation_farm(1)
        second = create_evaluation_farm(2)
        counts = {
            model: model.objects.count()
            for model in (Farm, User, Batch, DailyRecord, SaleEvent)
        }

        response = superuser_client.post(
            reverse(ARCHIVE),
            {"farm_ids": [first.farm.pk, staffed_farm.pk]},
            follow=True,
        )
        assert response.status_code == 200
        assert "Archived 1 evaluation farm(s) and deactivated 4 demo account(s)" in (
            response.content.decode()
        )

        assert {model: model.objects.count() for model in counts} == counts
        first.farm.refresh_from_db()
        second.farm.refresh_from_db()
        staffed_farm.refresh_from_db()
        assert first.farm.is_active is False
        assert second.farm.is_active is True
        assert staffed_farm.is_active is True
        assert not User.objects.filter(
            phone_number__in=demo_phone_numbers(1), is_active=True
        ).exists()
        assert User.objects.filter(phone_number__in=demo_phone_numbers(2), is_active=True).count() == 4
        owner.refresh_from_db()
        assert owner.is_active

        assert LogEntry.objects.filter(
            user=superuser, action_flag=CHANGE, object_id=str(first.farm.pk)
        ).exists()

        body = superuser_client.get(reverse(INDEX)).content.decode()
        assert "Archived" in body

    def test_nothing_selected(self, superuser_client):
        response = superuser_client.post(reverse(ARCHIVE), {}, follow=True)
        assert "Select at least one evaluation farm" in response.content.decode()


class TestSidebarAndRegistry:
    def test_link_in_the_system_hub_for_superusers_only(self, superuser_client, staff_client):
        from checkngo.admin_theme import SIDEBAR_NAVIGATION

        system = next(hub for hub in SIDEBAR_NAVIGATION if str(hub["title"]) == "System")
        assert "Evaluation farms" in [str(item["title"]) for item in system["items"]]

        url = reverse(INDEX)
        assert url in superuser_client.get(reverse("admin:index")).content.decode()
        assert url not in staff_client.get(reverse("admin:index")).content.decode()

    def test_no_farm_data_model_is_registered(self):
        registered = {model._meta.app_label for model in admin.site._registry}
        assert "production" not in registered
