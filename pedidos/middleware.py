from django.contrib import messages
from django.contrib.auth import logout
from django.http import JsonResponse
from django.shortcuts import redirect

from .sesiones import aplicar_cookie_dispositivo, validar_sesion_request


class SesionUnicaMiddleware:
    """Expulsa una sesión cuando otro dispositivo adquirió el usuario."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and not validar_sesion_request(request):
            logout(request)
            if request.path.startswith("/api/") or request.path.startswith("/admin/api/"):
                response = JsonResponse(
                    {
                        "success": False,
                        "codigo": "sesion_reemplazada",
                        "mensaje": "Tu sesión se inició en otro dispositivo. Vuelve a iniciar sesión para continuar.",
                    },
                    status=401,
                )
            else:
                messages.error(
                    request,
                    "Tu sesión se cerró porque la cuenta se inició en otro dispositivo.",
                )
                response = redirect("login")
            return aplicar_cookie_dispositivo(request, response)

        response = self.get_response(request)
        return aplicar_cookie_dispositivo(request, response)
