import hashlib
import re
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from proyecto.security import login_client_ip

from .models import SesionActiva


SESSION_TOKEN_KEY = "pedidos_active_session_token"
DEVICE_COOKIE_NAME = "pedidos_device_id"
DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def tiempo_vida_sesion():
    segundos = int(getattr(settings, "ACTIVE_SESSION_TTL_SECONDS", 600))
    # Nunca menos de dos heartbeats ni tanto como el spin-down (15 min).
    return timedelta(seconds=min(max(segundos, 120), 840))


def sesion_esta_vigente(sesion, ahora=None):
    ahora = ahora or timezone.now()
    return sesion.ultima_actividad >= ahora - tiempo_vida_sesion()


def direccion_ip(request):
    # Sólo el proxy local de Nginx puede aportar X-Real-IP. X-Forwarded-For
    # recibido desde Internet no es una identidad confiable para auditoría.
    return login_client_ip(request)


def describir_dispositivo(request):
    user_agent = request.META.get("HTTP_USER_AGENT", "").strip()
    return user_agent[:200] or "Navegador no identificado"


def dispositivo_id_request(request):
    candidato = request.POST.get("device_id", "") or request.COOKIES.get(DEVICE_COOKIE_NAME, "")
    candidato = candidato.strip()
    if DEVICE_ID_PATTERN.fullmatch(candidato):
        return candidato
    return secrets.token_urlsafe(24)


def marcar_cookie_dispositivo(request, dispositivo_id):
    request._pedidos_device_id_to_set = dispositivo_id


def aplicar_cookie_dispositivo(request, response):
    dispositivo_id = getattr(request, "_pedidos_device_id_to_set", "")
    if dispositivo_id:
        response.set_cookie(
            DEVICE_COOKIE_NAME,
            dispositivo_id,
            max_age=60 * 60 * 24 * 365,
            secure=settings.SESSION_COOKIE_SECURE,
            httponly=False,
            samesite="Lax",
        )
    return response


def hash_sesion(token):
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def intentar_iniciar_sesion(request, user, forzar=False):
    """Adquiere el arrendamiento bajo bloqueo o informa la sesión que lo impide."""

    ahora = timezone.now()
    dispositivo_id = dispositivo_id_request(request)
    with transaction.atomic():
        usuario_bloqueado = User.objects.select_for_update().get(pk=user.pk)
        sesion = SesionActiva.objects.select_for_update().filter(usuario=usuario_bloqueado).first()
        mismo_dispositivo = bool(
            sesion
            and sesion.dispositivo_id
            and sesion.dispositivo_id == dispositivo_id
        )
        if sesion and sesion_esta_vigente(sesion, ahora) and not mismo_dispositivo and not forzar:
            return False, sesion

        token = secrets.token_urlsafe(32)
        # El usuario se relee con bloqueo y pierde el atributo `.backend` que
        # añadió authenticate(). Con Axes + ModelBackend hay dos backends y
        # Django exige indicar explícitamente cuál estableció la identidad.
        login(
            request,
            usuario_bloqueado,
            backend=getattr(user, "backend", "django.contrib.auth.backends.ModelBackend"),
        )
        request.session[SESSION_TOKEN_KEY] = token
        request.session.modified = True
        SesionActiva.objects.update_or_create(
            usuario=usuario_bloqueado,
            defaults={
                "token": token,
                "dispositivo_id": dispositivo_id,
                "dispositivo": describir_dispositivo(request),
                "direccion_ip": direccion_ip(request),
                "iniciada_en": ahora,
                "ultima_actividad": ahora,
            },
        )
        marcar_cookie_dispositivo(request, dispositivo_id)
        return True, None


def validar_sesion_request(request):
    """Valida el token actual; adopta sesiones antiguas solo si no desplaza otra vigente."""

    token = request.session.get(SESSION_TOKEN_KEY, "")
    ahora = timezone.now()
    if token:
        sesion = SesionActiva.objects.filter(usuario=request.user, token=token).first()
        if sesion is None:
            return False
        if not sesion_esta_vigente(sesion, ahora):
            return False
        if sesion.ultima_actividad < ahora - timedelta(seconds=60):
            SesionActiva.objects.filter(pk=sesion.pk, token=token).update(ultima_actividad=ahora)
        return True

    dispositivo_id = dispositivo_id_request(request)
    with transaction.atomic():
        usuario_bloqueado = User.objects.select_for_update().get(pk=request.user.pk)
        sesion = SesionActiva.objects.select_for_update().filter(usuario=usuario_bloqueado).first()

        if sesion and sesion.dispositivo_id == dispositivo_id:
            request.session[SESSION_TOKEN_KEY] = sesion.token
            request.session.modified = True
            sesion.ultima_actividad = ahora
            sesion.save(update_fields=["ultima_actividad"])
            marcar_cookie_dispositivo(request, dispositivo_id)
            return True

        if sesion and sesion_esta_vigente(sesion, ahora):
            return False

        nuevo_token = secrets.token_urlsafe(32)
        SesionActiva.objects.update_or_create(
            usuario=usuario_bloqueado,
            defaults={
                "token": nuevo_token,
                "dispositivo_id": dispositivo_id,
                "dispositivo": describir_dispositivo(request),
                "direccion_ip": direccion_ip(request),
                "iniciada_en": ahora,
                "ultima_actividad": ahora,
            },
        )
        request.session[SESSION_TOKEN_KEY] = nuevo_token
        request.session.modified = True
        marcar_cookie_dispositivo(request, dispositivo_id)
        return True


def liberar_sesion(request):
    token = request.session.get(SESSION_TOKEN_KEY, "")
    if request.user.is_authenticated and token:
        SesionActiva.objects.filter(usuario=request.user, token=token).delete()
