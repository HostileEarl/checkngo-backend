# accounts/throttles.py
from rest_framework.throttling import SimpleRateThrottle

from .models import UserManager


class PhoneLoginThrottle(SimpleRateThrottle):
    """
    Rate-limit login attempts PER PHONE NUMBER, not per IP.

    IP throttling alone fails here in both directions: farm staff often share
    one mobile connection or a rural NAT, so a per-IP limit punishes innocent
    co-workers; and an attacker with rotating IPs slips past it entirely.
    Keying on the targeted account fixes both.
    """

    scope = "phone_login"

    def get_cache_key(self, request, view):
        # Keyed on the number the way the login reads it: the serializer
        # field trims it and PhonePinTokenSerializer.validate()
        # runs UserManager.normalize_phone(), so "+63 917 111 2222" and
        # "+639171112222" are one account and must share one count.
        phone = UserManager.normalize_phone((request.data.get("phone_number") or "").strip())
        if not phone:
            return None  # No target — nothing to throttle.
        return self.cache_format % {"scope": self.scope, "ident": phone}


class LoginIPThrottle(SimpleRateThrottle):
    """
    Second layer, keyed on IP.

    Stops one source spraying a single PIN across many phone numbers —
    an attack the per-phone throttle above cannot see, because each
    individual account only gets one attempt.
    """

    scope = "login_ip"

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }


class InvitationAcceptThrottle(SimpleRateThrottle):
    """
    The public accept endpoint. Unauthenticated by necessity, so IP is the
    only identifier available.
    """

    scope = "invite_accept"

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }


class CredentialChangeThrottle(SimpleRateThrottle):
    """
    Changing a PIN requires the current one, so this endpoint is a PIN
    oracle for anyone holding a stolen access token. Throttle per user.
    """

    scope = "credential_change"

    def get_cache_key(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return None
        return self.cache_format % {"scope": self.scope, "ident": request.user.pk}


class AccessRequestThrottle(SimpleRateThrottle):
    """
    The landing page's public "Request access" form. Anonymous, so IP is
    the only identifier; the IP lives only in the throttle cache key, never
    on the saved AccessRequest.
    """

    scope = "access_request"

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }
