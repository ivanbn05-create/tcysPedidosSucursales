"""Django Admin sólo admite sesiones verificadas por TOTP."""

from django_otp.admin import OTPAdminSite
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.models import Device
from django.contrib.auth import get_user

from pedidos.mfa import marcar_sesion_verificada


class AdminSeguro(OTPAdminSite):
    def __init__(self, name="admin"):
        super().__init__(name=name)

    def login(self, request, extra_context=None):
        verificado_antes = bool(
            request.user.is_authenticated
            and getattr(request.user, "is_verified", lambda: False)()
        )
        respuesta = super().login(request, extra_context)
        if (
            request.method == "POST"
            and not verificado_antes
            and respuesta.status_code in (301, 302, 303)
        ):
            usuario = get_user(request)
            device_id = request.session.get(DEVICE_ID_SESSION_KEY, "")
            dispositivo = Device.from_persistent_id(device_id, for_verify=True) if device_id else None
            if (
                usuario.is_authenticated
                and dispositivo is not None
                and dispositivo.user_id == usuario.pk
                and dispositivo.confirmed
            ):
                marcar_sesion_verificada(request, usuario, dispositivo)
        return respuesta
