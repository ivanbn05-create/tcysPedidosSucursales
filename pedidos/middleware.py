from django.contrib import messages
from django.contrib.auth import logout
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse

from .sesiones import aplicar_cookie_dispositivo, liberar_sesion, validar_sesion_request
from .mfa import (
    STAMP_KEY,
    mfa_activo,
    mfa_reciente,
    mfa_requerido,
    sesion_mfa_valida,
)


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


class MFAEnforcementMiddleware:
    """Bloquea toda sesión privilegiada sin OTP, incluso rutas no decoradas."""

    EXENTAS = (
        "/login/", "/logout/", "/privacidad/",
        "/mfa/login/", "/mfa/revalidar/", "/mfa/recuperacion/",
    )
    PREFIJOS_EXENTOS = ("/static/", "/api/v1/pos/", "/api/v2/pos/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not mfa_activo() or request.path in self.EXENTAS or request.path.startswith(self.PREFIJOS_EXENTOS):
            return self.get_response(request)
        if not request.user.is_authenticated:
            return self.get_response(request)

        if not mfa_requerido(request.user):
            # Una cuenta degradada deja de requerir OTP, pero debe perder la
            # sesión que obtuvo cuando tenía privilegios.
            if STAMP_KEY in request.session:
                liberar_sesion(request)
                logout(request)
                return redirect("login")
            return self.get_response(request)

        if not sesion_mfa_valida(request):
            liberar_sesion(request)
            logout(request)
            if request.path.startswith("/api/") or request.path.startswith("/admin/api/"):
                return JsonResponse({"success": False, "codigo": "mfa_requerido"}, status=401)
            return redirect("login")

        if request.method not in ("GET", "HEAD", "OPTIONS", "TRACE") and request.path.startswith(("/admin/", "/django-admin/")) and not mfa_reciente(request):
            if request.path.startswith("/admin/api/"):
                return JsonResponse({"success": False, "codigo": "mfa_reciente_requerida"}, status=403)
            return redirect(f"{reverse('mfa_revalidar')}?next={reverse('admin_dashboard')}")

        return self.get_response(request)
