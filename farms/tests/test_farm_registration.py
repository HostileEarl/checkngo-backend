# farms/tests/test_farm_registration.py
"""
POST /api/farms/ — who may register a farm.

Registering a farm makes the caller its OWNER (farms.signals grants the
membership), so only an owner account may do it. Without that check any
rotated account could register one, a buyer included, and so hold a farm
membership that FarmMembership.clean() says an external partner must never
have. Found while generating docs/PERMISSION_MATRIX.md.
"""
import pytest
from django.urls import reverse

from accounts.models import User
from farms.models import Farm, FarmMembership

pytestmark = pytest.mark.django_db

URL = reverse("farms:farm-list")
BODY = {"name": "New Farm", "municipality": "Lipa", "province": "Batangas"}


class TestOwnerAccounts:
    def test_an_owner_with_no_farm_registers_the_first_one(self, auth):
        """The setup wizard's step 1 (checkngo-frontend FarmSetup.tsx)."""
        owner = User.objects.create_user(
            "+639170004001", "400100", full_name="Brand New Owner",
            role=User.Role.OWNER, must_change_credential=False,
        )
        response = auth(owner).post(URL, BODY, format="json")
        assert response.status_code == 201, response.data
        farm = Farm.objects.get(pk=response.data["id"])
        assert farm.owner == owner
        assert FarmMembership.objects.get(farm=farm, user=owner).role == FarmMembership.Role.OWNER

    def test_an_owner_with_a_farm_may_register_another(self, auth, owner, farm):
        response = auth(owner).post(URL, BODY, format="json")
        assert response.status_code == 201
        assert Farm.objects.filter(owner=owner).count() == 2


class TestEveryoneElse:
    @pytest.mark.parametrize("who", ["consumer", "manager", "worker"])
    def test_is_refused_and_nothing_is_created(self, request, auth, who):
        user = request.getfixturevalue(who)
        response = auth(user).post(URL, BODY, format="json")
        assert response.status_code == 403
        assert response.data["detail"] == "Only a farm owner account can register a farm."
        assert not Farm.objects.filter(name=BODY["name"]).exists()
        assert not FarmMembership.objects.filter(user=user).exists()

    @pytest.mark.parametrize("who", ["consumer", "manager", "worker"])
    def test_can_still_list_their_farms(self, request, auth, who):
        response = auth(request.getfixturevalue(who)).get(URL)
        assert response.status_code == 200

    def test_an_owner_who_has_not_rotated_their_pin_is_refused(self, auth):
        gated = User.objects.create_user(
            "+639170004002", "400200", full_name="Gated Owner", role=User.Role.OWNER
        )
        assert gated.must_change_credential
        response = auth(gated).post(URL, BODY, format="json")
        assert response.status_code == 403
        assert not Farm.objects.filter(owner=gated).exists()

    def test_anonymous_is_refused(self, api):
        assert api.post(URL, BODY, format="json").status_code == 401
