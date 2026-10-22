# accounts/demo_numbers.py
"""
The phone numbers reserved for evaluation-farm demo accounts — defined once.

    +63999 NN 0000 R     NN = session 01-99, R = role digit 1-4
                         (1 owner, 2 manager, 3 worker, 4 buyer)

production/evaluation.py mints accounts on these numbers, and
notifications/sms.py refuses to text them: 0999 is a live Smart prefix, so
a text to a demo account could reach a real stranger's phone.

Deliberately free of app imports (no models, no settings), so both
notifications and production can import it without importing each other.
"""
import re

DEMO_NUMBER_PREFIX = "+63999"
MIN_SESSION_NUMBER = 1
MAX_SESSION_NUMBER = 99
ROLE_DIGITS = (1, 2, 3, 4)

# E.164 (+63...), bare country code (63...) or local (09...) — send_sms
# turns E.164 into the local form before it reaches the provider, and a
# caller could pass either.
_RESERVED_RE = re.compile(r"^(?:\+?63|0)999(?:0[1-9]|[1-9]\d)0000[1-4]$")


def demo_phone_number(session_number, role_digit):
    """+63999 NN 0000 R, e.g. session 3's owner is +639990300001."""
    if not MIN_SESSION_NUMBER <= session_number <= MAX_SESSION_NUMBER:
        raise ValueError(f"Session numbers run from 01 to 99, not {session_number}.")
    if role_digit not in ROLE_DIGITS:
        raise ValueError(f"Role digits run from 1 to 4, not {role_digit}.")
    return f"{DEMO_NUMBER_PREFIX}{session_number:02d}0000{role_digit}"


def is_reserved_demo_number(phone_number):
    """True for any evaluation-farm demo number, in E.164 or local form."""
    if not phone_number:
        return False
    compact = str(phone_number).replace(" ", "").replace("-", "")
    return bool(_RESERVED_RE.match(compact))
