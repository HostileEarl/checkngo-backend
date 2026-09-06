# accounts/services.py
"""
The one place a farm-owner account is minted.

Both the admin's "Farm owner accounts" add form and the ``make_test_owner``
management command call :func:`issue_owner_account`. A single code path is the
point: an owner issued here is identical in every respect to one produced when
an invitee accepts an invitation — a system-generated numeric PIN, stored only
as the password hash, and ``must_change_credential`` left True so the
forced-rotation gate fires on first login.
"""
from django.db import transaction

from .models import User, generate_pin


@transaction.atomic
def issue_owner_account(*, full_name, phone_number, email=None, pin=None):
    """
    Create an OWNER ``User`` with a system-generated 6-digit PIN.

    Returns ``(user, raw_pin)``. The PIN is recoverable only from this return
    value — it is never stored in the clear and cannot be read back later.

    ``pin`` is an escape hatch for local testing (``make_test_owner --pin``);
    leave it None in every real call so a cryptographically random PIN is used.

    A duplicate phone number raises ``django.core.exceptions.ValidationError``
    (via the model's own ``full_clean``), not an IntegrityError.
    """
    raw_pin = pin or generate_pin()

    user = User.objects.create_user(
        phone_number=phone_number,
        password=raw_pin,
        full_name=full_name,
        email=email or None,
        role=User.Role.OWNER,
    )

    # create_user() defaults is_staff=False; the model default for
    # must_change_credential is True. Verify rather than set, so a future
    # change to either default trips here instead of silently issuing an
    # ungated or staff-privileged owner.
    if user.is_staff or not user.must_change_credential:
        raise RuntimeError(
            "issue_owner_account produced an ungated or staff owner — "
            "check the User model / manager defaults."
        )

    return user, raw_pin
