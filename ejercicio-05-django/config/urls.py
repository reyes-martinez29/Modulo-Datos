"""
config/urls.py — URLconf raíz del proyecto Django.

Incluye las rutas de la app transactions bajo el prefijo /api/v1/
y el Django Admin bajo /admin/.

La ruta de obtención de token de DRF (POST /api/v1/auth/token/)
se registra aquí usando la view built-in de DRF para obtener tokens
sin necesidad de implementarla manualmente.
"""

from django.contrib import admin
from django.urls import include, path
from rest_framework.authtoken.views import obtain_auth_token

urlpatterns = [
    # Django Admin
    path("admin/", admin.site.urls),

    # Obtención de token — POST con username + password
    # Retorna: {"token": "abc123..."}
    # Uso: Authorization: Token abc123...
    path("api/v1/auth/token/", obtain_auth_token, name="obtain-token"),

    # Endpoints del sistema de transacciones
    path("api/v1/", include("transactions.urls")),
]