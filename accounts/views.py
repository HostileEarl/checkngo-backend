# accounts/views.py
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from .serializers import (
    CredentialChangeSerializer,
    LogoutSerializer,
    PhonePinTokenSerializer,
    UserSerializer,
)


class PhonePinLoginView(TokenObtainPairView):
    """POST /api/auth/login/ → {access, refresh, user, must_change_credential, memberships}"""

    serializer_class = PhonePinTokenSerializer


class MeView(APIView):
    """GET /api/auth/me/ — current user, for app boot and session restore."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)


class CredentialChangeView(APIView):
    """POST /api/auth/credential/change/ — clears must_change_credential, revokes old sessions."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = CredentialChangeSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {"detail": "PIN updated.", "must_change_credential": False},
            status=status.HTTP_200_OK,
        )


class LogoutView(APIView):
    """POST /api/auth/logout/ — revoke a single device's refresh token."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_205_RESET_CONTENT)