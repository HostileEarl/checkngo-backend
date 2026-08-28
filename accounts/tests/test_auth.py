# accounts/tests/test_auth.py
import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


class TestPhonePinLogin:
    def test_login_with_pin_field(self, api, owner):
        response = api.post(
            reverse("accounts:login"),
            {"phone_number": owner.phone_number, "pin": "111111"},
            format="json",
        )
        assert response.status_code == 200
        assert "access" in response.data
        assert "refresh" in response.data
        assert response.data["must_change_credential"] is False

    def test_password_field_is_rejected(self, api, owner):
        response = api.post(
            reverse("accounts:login"),
            {"phone_number": owner.phone_number, "password": "111111"},
            format="json",
        )
        assert response.status_code == 400

    def test_phone_is_normalized_on_login(self, api, owner):
        response = api.post(
            reverse("accounts:login"),
            {"phone_number": "+63 917 111 2222", "pin": "111111"},
            format="json",
        )
        assert response.status_code == 200

    def test_wrong_pin_rejected(self, api, owner):
        response = api.post(
            reverse("accounts:login"),
            {"phone_number": owner.phone_number, "pin": "000000"},
            format="json",
        )
        assert response.status_code == 401

    def test_inactive_user_rejected(self, api, owner):
        owner.is_active = False
        owner.save(update_fields=["is_active"])
        response = api.post(
            reverse("accounts:login"),
            {"phone_number": owner.phone_number, "pin": "111111"},
            format="json",
        )
        assert response.status_code == 401

    def test_login_returns_memberships(self, api, worker, staffed_farm):
        response = api.post(
            reverse("accounts:login"),
            {"phone_number": worker.phone_number, "pin": "333333"},
            format="json",
        )
        assert response.status_code == 200
        assert len(response.data["memberships"]) == 1
        assert response.data["memberships"][0]["farm_name"] == "Santos Layer Farm"


class TestRotationGate:
    def test_gated_user_blocked_from_farms(self, auth, gated_worker, staffed_farm):
        client = auth(gated_worker)
        assert client.get(reverse("farms:farm-list")).status_code == 403

    def test_gated_user_can_read_me(self, auth, gated_worker):
        client = auth(gated_worker)
        response = client.get(reverse("accounts:me"))
        assert response.status_code == 200
        assert response.data["must_change_credential"] is True

    def test_rotation_opens_the_gate(self, auth, gated_worker, staffed_farm):
        client = auth(gated_worker)
        response = client.post(
            reverse("accounts:credential-change"),
            {
                "current_pin": "444444",
                "new_pin": "654321",
                "confirm_pin": "654321",
            },
            format="json",
        )
        assert response.status_code == 200

        gated_worker.refresh_from_db()
        assert gated_worker.must_change_credential is False
        assert gated_worker.credential_changed_at is not None

        client = auth(gated_worker)
        assert client.get(reverse("farms:farm-list")).status_code == 200

    def test_login_after_rotation_reports_gate_cleared(self, api, auth, gated_worker):
        """
        End-to-end: rotate the PIN, then log in fresh with the NEW one. The
        login response must report the gate as cleared — this is what the
        client reads to decide whether to route to /change-pin.
        """
        auth(gated_worker).post(
            reverse("accounts:credential-change"),
            {
                "current_pin": "444444",
                "new_pin": "654321",
                "confirm_pin": "654321",
            },
            format="json",
        )

        api.force_authenticate(user=None)
        response = api.post(
            reverse("accounts:login"),
            {"phone_number": gated_worker.phone_number, "pin": "654321"},
            format="json",
        )
        assert response.status_code == 200
        assert response.data["must_change_credential"] is False
        assert response.data["user"]["must_change_credential"] is False

    def test_new_pin_must_differ(self, auth, gated_worker):
        client = auth(gated_worker)
        response = client.post(
            reverse("accounts:credential-change"),
            {
                "current_pin": "444444",
                "new_pin": "444444",
                "confirm_pin": "444444",
            },
            format="json",
        )
        assert response.status_code == 400

    def test_wrong_current_pin_rejected(self, auth, gated_worker):
        client = auth(gated_worker)
        response = client.post(
            reverse("accounts:credential-change"),
            {
                "current_pin": "000000",
                "new_pin": "654321",
                "confirm_pin": "654321",
            },
            format="json",
        )
        assert response.status_code == 400


class TestSessionRevocation:
    def test_pin_change_kills_existing_sessions(self, api, worker):
        login = api.post(
            reverse("accounts:login"),
            {"phone_number": worker.phone_number, "pin": "333333"},
            format="json",
        )
        refresh = login.data["refresh"]

        api.force_authenticate(user=worker)
        api.post(
            reverse("accounts:credential-change"),
            {"current_pin": "333333", "new_pin": "888888", "confirm_pin": "888888"},
            format="json",
        )

        api.force_authenticate(user=None)
        response = api.post(
            reverse("accounts:token-refresh"), {"refresh": refresh}, format="json"
        )
        assert response.status_code == 401

    def test_logout_blacklists_refresh(self, api, worker):
        login = api.post(
            reverse("accounts:login"),
            {"phone_number": worker.phone_number, "pin": "333333"},
            format="json",
        )
        refresh = login.data["refresh"]

        api.force_authenticate(user=worker)
        assert api.post(
            reverse("accounts:logout"), {"refresh": refresh}, format="json"
        ).status_code == 205

        api.force_authenticate(user=None)
        assert api.post(
            reverse("accounts:token-refresh"), {"refresh": refresh}, format="json"
        ).status_code == 401