"""
Dev-only fallback authentication for frisian-mcp testing.

When Claude.ai (or any client) doesn't send a Bearer token, this class
authenticates the request as the first Django superuser so that paperless-ngx
ViewSet permission checks pass.  Never use this in production.
"""

from rest_framework.authentication import BaseAuthentication


class DevFallbackSuperuserAuthentication(BaseAuthentication):
    def authenticate(self, request):
        from django.contrib.auth import get_user_model  # noqa: PLC0415
        User = get_user_model()
        user = User.objects.filter(is_superuser=True).order_by("pk").first()
        if user is None:
            return None
        return (user, None)
