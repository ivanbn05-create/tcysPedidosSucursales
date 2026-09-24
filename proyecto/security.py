"""Política de bloqueo de login sin depender de cabeceras de proxy no fiables."""

from ipaddress import ip_address

from django.http import HttpResponse


def login_client_ip(request):
    """Sólo confía en X-Real-IP cuando la conexión inmediata es loopback/Nginx."""

    remoto = request.META.get("REMOTE_ADDR", "")
    try:
        direccion = ip_address(remoto)
    except ValueError:
        return None
    if direccion.is_loopback:
        reenviada = request.META.get("HTTP_X_REAL_IP", "")
        try:
            return str(ip_address(reenviada))
        except ValueError:
            pass
    return str(direccion)


def login_lockout(request, response, credentials, *args, **kwargs):
    """No revela si existe la cuenta ni qué factor agotó sus intentos."""

    return HttpResponse(
        "No se pudo iniciar sesión. Intenta de nuevo más tarde.",
        status=429,
        content_type="text/plain; charset=utf-8",
    )
