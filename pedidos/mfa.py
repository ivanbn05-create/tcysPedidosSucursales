"""MFA humano: TOTP con comprobación de uso único y recuperación supervisada.

La semilla TOTP pertenece a django-otp; jamás se registra ni se transmite fuera
del enrolamiento explícito. Los códigos de recuperación sólo viven como hash.
"""

import base64
import hashlib
import hmac
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.db import transaction
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from django_otp import login as otp_login
from django_otp import verify_token as otp_verify_token
from django_otp.plugins.otp_totp.models import TOTPDevice

from .models import MFAEstado, MFACodigoRecuperacion, SesionActiva


PENDING_KEY = "pedidos_mfa_pending"
RESET_KEY = "pedidos_mfa_reset_pending"
STAMP_KEY = "pedidos_mfa_credenciales"
RECENT_KEY = "pedidos_mfa_reciente"
PRINT_GROUP_NAME = "Operador de impresion"
ERROR_GENERICO = "No fue posible verificar las credenciales. Inténtalo de nuevo."


def mfa_activo():
    return bool(getattr(settings, "MFA_ENFORCE", True))


def mfa_requerido(user):
    return bool(
        user.is_authenticated
        and (
            user.is_staff
            or user.is_superuser
            or user.groups.filter(name=PRINT_GROUP_NAME).exists()
        )
    )


def _tiempo_pendiente():
    return max(60, min(600, int(getattr(settings, "MFA_PENDING_SECONDS", 300))))


def _tiempo_reciente():
    return max(60, min(900, int(getattr(settings, "MFA_STEP_UP_SECONDS", 300))))


def _version(user):
    return MFAEstado.objects.filter(usuario=user).values_list("version", flat=True).first() or 0


def huella_credenciales(user):
    """No persiste contraseña, semilla ni grupos legibles en la sesión."""
    grupos = ",".join(map(str, user.groups.order_by("pk").values_list("pk", flat=True)))
    material = (
        f"{user.pk}:{user.password}:{int(user.is_active)}:"
        f"{int(user.is_staff)}:{int(user.is_superuser)}:{grupos}:{_version(user)}"
    )
    return salted_hmac("pedidos.mfa.credenciales.v1", material).hexdigest()


def marcar_sesion_verificada(request, user, device):
    if device.user_id != user.pk or not device.confirmed:
        raise ValueError("Dispositivo MFA no confirmado o ajeno al usuario")
    otp_login(request, device)
    request.session[STAMP_KEY] = huella_credenciales(user)
    request.session[RECENT_KEY] = timezone.now().timestamp()
    request.session.modified = True


def sesion_mfa_valida(request):
    user = request.user
    if not user.is_authenticated or not getattr(user, "is_verified", lambda: False)():
        return False
    device = getattr(user, "otp_device", None)
    return bool(
        device
        and device.user_id == user.pk
        and device.confirmed
        and hmac.compare_digest(
            str(request.session.get(STAMP_KEY, "")), huella_credenciales(user)
        )
    )


def mfa_reciente(request):
    if not sesion_mfa_valida(request):
        return False
    try:
        instante = float(request.session.get(RECENT_KEY, 0))
    except (TypeError, ValueError):
        return False
    edad = timezone.now().timestamp() - instante
    return 0 <= edad <= _tiempo_reciente()


def _codigo_normalizado(codigo):
    return codigo.strip().replace(" ", "").upper()[:80]


def _digest_codigo(codigo):
    return hashlib.sha256(codigo.encode("ascii")).hexdigest()


def generar_codigos_recuperacion(user, cantidad=8):
    """Rota los códigos; el retorno existe sólo en memoria para mostrar una vez."""
    if not 1 <= cantidad <= 10:
        raise ValueError("Cantidad inválida")
    codigos = ["RC-" + base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=") for _ in range(cantidad)]
    with transaction.atomic():
        MFACodigoRecuperacion.objects.filter(usuario=user).delete()
        MFACodigoRecuperacion.objects.bulk_create(
            MFACodigoRecuperacion(usuario=user, digest=_digest_codigo(codigo))
            for codigo in codigos
        )
    return codigos


def _estado_bloqueado(estado):
    return bool(estado.bloqueado_hasta and estado.bloqueado_hasta > timezone.now())


def _anotar_fallo(estado):
    estado.fallos += 1
    limite = max(3, min(10, int(getattr(settings, "MFA_MAX_ATTEMPTS", 5))))
    if estado.fallos >= limite:
        segundos = max(60, min(3600, int(getattr(settings, "MFA_LOCK_SECONDS", 900))))
        estado.bloqueado_hasta = timezone.now() + timedelta(seconds=segundos)
        estado.fallos = 0
    estado.save(update_fields=["fallos", "bloqueado_hasta"])


def verificar_factor(user, codigo):
    """Retorna (dispositivo, recuperacion); serializa intentos entre workers."""
    codigo = _codigo_normalizado(codigo)
    with transaction.atomic():
        estado, _ = MFAEstado.objects.get_or_create(usuario=user)
        estado = MFAEstado.objects.select_for_update().get(pk=estado.pk)
        if _estado_bloqueado(estado):
            return None, False

        if codigo.startswith("RC-") and len(codigo) == 35:
            digest = _digest_codigo(codigo)
            registro = (
                MFACodigoRecuperacion.objects.select_for_update()
                .filter(usuario=user, digest=digest, usado_en__isnull=True)
                .first()
            )
            if registro:
                registro.usado_en = timezone.now()
                registro.save(update_fields=["usado_en"])
                estado.fallos = 0
                estado.bloqueado_hasta = None
                estado.save(update_fields=["fallos", "bloqueado_hasta"])
                return None, True
        elif len(codigo) == 6 and codigo.isascii() and codigo.isdigit():
            dispositivo = (
                TOTPDevice.objects.filter(user=user, confirmed=True)
                .order_by("pk")
                .first()
            )
            if dispositivo and otp_verify_token(user, dispositivo.persistent_id, codigo):
                estado.fallos = 0
                estado.bloqueado_hasta = None
                estado.save(update_fields=["fallos", "bloqueado_hasta"])
                return dispositivo, False

        _anotar_fallo(estado)
        return None, False


def iniciar_desafio(request, user):
    """Después de contraseña válida, aún NO crea una sesión autenticada."""
    MFAEstado.objects.get_or_create(usuario=user)
    request.session.cycle_key()
    request.session[PENDING_KEY] = {
        "usuario": user.pk,
        "huella": huella_credenciales(user),
        "creado": timezone.now().timestamp(),
    }
    request.session.pop(RESET_KEY, None)
    request.session.pop("pedidos_takeover", None)
    request.session.modified = True


def _usuario_pendiente(request, clave=PENDING_KEY):
    pendiente = request.session.get(clave, {})
    try:
        edad = timezone.now().timestamp() - float(pendiente["creado"])
        usuario_id = int(pendiente["usuario"])
        huella = pendiente["huella"]
    except (KeyError, TypeError, ValueError):
        return None
    if edad < 0 or edad > _tiempo_pendiente():
        return None
    usuario = User.objects.filter(pk=usuario_id, is_active=True).first()
    if not usuario or not mfa_requerido(usuario):
        return None
    if not hmac.compare_digest(str(huella), huella_credenciales(usuario)):
        return None
    return usuario


def _redireccion_segura(request, valor, fallback="home"):
    if valor and url_has_allowed_host_and_scheme(
        valor, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ) and valor.startswith("/") and not valor.startswith("//"):
        return valor
    return reverse(fallback)


@never_cache
@require_http_methods(["GET", "POST"])
def desafio_login(request):
    if request.user.is_authenticated:
        return redirect("home")
    usuario = _usuario_pendiente(request)
    if not usuario:
        request.session.pop(PENDING_KEY, None)
        messages.error(request, ERROR_GENERICO)
        return redirect("login")
    if request.method == "POST":
        dispositivo, recuperacion = verificar_factor(usuario, request.POST.get("codigo", ""))
        if recuperacion:
            request.session[RESET_KEY] = request.session.pop(PENDING_KEY)
            request.session.cycle_key()
            return redirect("mfa_recuperacion")
        if dispositivo:
            from .sesiones import intentar_iniciar_sesion

            iniciada, conflicto = intentar_iniciar_sesion(request, usuario)
            if iniciada:
                marcar_sesion_verificada(request, usuario, dispositivo)
                request.session.pop(PENDING_KEY, None)
                return redirect("admin_dashboard")
            request.session["pedidos_takeover"] = {
                "user_id": usuario.pk,
                "creado_en": timezone.now().timestamp(),
                "huella": huella_credenciales(usuario),
                "mfa_device_id": dispositivo.pk,
            }
            request.session.pop(PENDING_KEY, None)
            request.session.cycle_key()
            return render(
                request,
                "pedidos/login.html",
                {"sesion_conflictiva": True, "dispositivo_conflictivo": conflicto.dispositivo,
                 "ultima_actividad_conflictiva": conflicto.ultima_actividad},
            )
        messages.error(request, ERROR_GENERICO)
    return render(request, "pedidos/login.html", {"mfa_desafio": True})


@never_cache
@require_http_methods(["GET", "POST"])
def revalidar_mfa(request):
    if not request.user.is_authenticated or not mfa_requerido(request.user):
        return redirect("login")
    if not sesion_mfa_valida(request):
        return redirect("login")
    destino = _redireccion_segura(request, request.POST.get("next") or request.GET.get("next"), "admin_dashboard")
    if request.method == "POST":
        usuario = authenticate(
            request,
            username=request.user.get_username(),
            password=request.POST.get("password", ""),
        )
        if usuario and usuario.pk == request.user.pk:
            dispositivo, recuperacion = verificar_factor(usuario, request.POST.get("codigo", ""))
            if dispositivo and not recuperacion:
                marcar_sesion_verificada(request, usuario, dispositivo)
                return redirect(destino)
        messages.error(request, ERROR_GENERICO)
    return render(request, "pedidos/login.html", {"mfa_revalidar": True, "mfa_next": destino})


@never_cache
@require_http_methods(["GET", "POST"])
def recuperar_mfa(request):
    """Un código consumido permite sustituir el TOTP, pero nunca entrar sin él."""
    if request.user.is_authenticated:
        return redirect("home")
    usuario = _usuario_pendiente(request, RESET_KEY)
    if not usuario:
        request.session.pop(RESET_KEY, None)
        messages.error(request, ERROR_GENERICO)
        return redirect("login")

    pendiente = request.session[RESET_KEY]
    dispositivo = TOTPDevice.objects.filter(
        pk=pendiente.get("nuevo_dispositivo"), user=usuario, confirmed=False
    ).first()
    if request.method == "POST" and request.POST.get("action") == "iniciar":
        if dispositivo:
            dispositivo.delete()
        dispositivo = TOTPDevice.objects.create(user=usuario, name="principal", confirmed=False)
        pendiente["nuevo_dispositivo"] = dispositivo.pk
        request.session[RESET_KEY] = pendiente
    elif request.method == "POST" and request.POST.get("action") == "confirmar":
        codigo = request.POST.get("codigo", "").strip()
        if dispositivo and len(codigo) == 6 and codigo.isascii() and codigo.isdigit():
            with transaction.atomic():
                estado = MFAEstado.objects.select_for_update().get(usuario=usuario)
                if not _estado_bloqueado(estado):
                    dispositivo = TOTPDevice.objects.select_for_update().get(pk=dispositivo.pk)
                    if dispositivo.verify_token(codigo):
                        dispositivo.confirmed = True
                        dispositivo.save(update_fields=["confirmed"])
                        TOTPDevice.objects.filter(user=usuario).exclude(pk=dispositivo.pk).delete()
                        codigos = generar_codigos_recuperacion(usuario)
                        SesionActiva.objects.filter(usuario=usuario).delete()
                        estado.version += 1
                        estado.fallos = 0
                        estado.bloqueado_hasta = None
                        estado.save(update_fields=["version", "fallos", "bloqueado_hasta"])
                    else:
                        _anotar_fallo(estado)
                        codigos = None
                else:
                    codigos = None
            if codigos:
                from .sesiones import intentar_iniciar_sesion

                iniciada, _ = intentar_iniciar_sesion(request, usuario, forzar=True)
                if iniciada:
                    marcar_sesion_verificada(request, usuario, dispositivo)
                    request.session.pop(RESET_KEY, None)
                    return render(request, "pedidos/login.html", {"mfa_codigos_nuevos": codigos})
        messages.error(request, ERROR_GENERICO)

    return render(
        request,
        "pedidos/login.html",
        {
            "mfa_recuperacion": True,
            "mfa_clave": base64.b32encode(dispositivo.bin_key).decode("ascii") if dispositivo else "",
        },
    )
