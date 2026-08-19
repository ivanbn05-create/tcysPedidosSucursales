from django.contrib.auth.signals import user_logged_out
from django.dispatch import receiver

from .models import SesionActiva
from .sesiones import SESSION_TOKEN_KEY


@receiver(user_logged_out)
def liberar_arrendamiento_al_cerrar_sesion(sender, request, user, **kwargs):
    """Libera también cierres hechos fuera de nuestra vista (admin/tests)."""

    if request is None or user is None:
        return
    token = request.session.get(SESSION_TOKEN_KEY, "")
    if token:
        SesionActiva.objects.filter(usuario=user, token=token).delete()
