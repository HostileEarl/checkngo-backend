# farms/apps.py
from django.apps import AppConfig


class FarmsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "farms"

    def ready(self):
        from . import signals  # noqa: F401 