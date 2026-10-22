# accounts/tests/test_demo_numbers.py
"""
The reserved evaluation-farm demo numbers, defined once in
accounts/demo_numbers.py: what production/evaluation.py mints and what
notifications/sms.py refuses must be exactly the same set.
"""
import pytest

from accounts.demo_numbers import demo_phone_number, is_reserved_demo_number
from accounts.models import e164_validator
from production import evaluation


class TestDemoNumbers:
    def test_every_minted_number_is_reserved_and_valid_e164(self):
        minted = [
            demo_phone_number(session, digit)
            for session in range(1, 100)
            for digit in (1, 2, 3, 4)
        ]
        assert len(set(minted)) == 396
        for number in minted:
            assert is_reserved_demo_number(number), number
            e164_validator(number)

    def test_the_evaluation_service_uses_the_shared_definition(self):
        assert evaluation.demo_phone_number is demo_phone_number
        assert evaluation.demo_phone_numbers(3) == [
            "+639990300001", "+639990300002", "+639990300003", "+639990300004",
        ]

    @pytest.mark.parametrize(
        "number",
        [
            "+639990000001", "+639990300000", "+639990300005", "+6399903000011",
            "+63999030000", "+639990310001", "+639171112222", "+649990300001",
            "", None,
        ],
    )
    def test_near_misses_are_not_reserved(self, number):
        assert not is_reserved_demo_number(number)

    @pytest.mark.parametrize("session,digit", [(0, 1), (100, 1), (3, 0), (3, 5)])
    def test_out_of_range_parts_are_refused(self, session, digit):
        with pytest.raises(ValueError):
            demo_phone_number(session, digit)
