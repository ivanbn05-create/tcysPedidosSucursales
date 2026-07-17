"""Management command que envía el recordatorio diario de pedidos.

Se ejecuta con:

    python manage.py enviar_recordatorios           # respeta día/horario configurado
    python manage.py enviar_recordatorios --test     # simula, no manda correos reales
    python manage.py enviar_recordatorios --sucursal "Aguilas"
    python manage.py enviar_recordatorios --fuerza    # ignora día configurado y el flag
                                                       # recordatorios_habilitados

Nota sobre cómo se dispara automáticamente (ver también CLAUDE.md):
Mientras el proyecto viva en Render usamos APScheduler (pedidos/scheduler.py,
arrancado desde pedidos/apps.py) para llamar a este mismo comando todos los
días. Cuando migremos a un VPS, cambiaremos a un cron job nativo del SO que
invoque este comando directamente — el comando en sí no cambia.
"""

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.core.management.base import BaseCommand, CommandError
from django.template.loader import render_to_string
from django.utils import timezone

from pedidos.models import Configuracion, LogRecordatorio, SucursalCliente

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Envía el recordatorio diario de pedidos a sucursales y clientes con correo configurado."

    def add_arguments(self, parser):
        parser.add_argument(
            "--test",
            action="store_true",
            help="No envía correos reales: solo simula e imprime en consola.",
        )
        parser.add_argument(
            "--sucursal",
            type=str,
            default=None,
            help="Enviar solo a la sucursal/cliente con este nombre exacto (ignora mayúsculas).",
        )
        parser.add_argument(
            "--fuerza",
            action="store_true",
            help="Ignora el día configurado y el flag recordatorios_habilitados.",
        )

    def handle(self, *args, **options):
        modo_test = options["test"]
        solo_sucursal = options["sucursal"]
        forzar = options["fuerza"]

        config = Configuracion.get_solo()

        if not forzar:
            if not config.recordatorios_habilitados:
                self.stdout.write(
                    self.style.WARNING(
                        "Recordatorios deshabilitados en Configuración. Usa --fuerza para enviar de todas formas."
                    )
                )
                return

            hoy_iso = timezone.localdate().isoweekday()
            if hoy_iso not in config.dias_recordatorio_lista():
                self.stdout.write(
                    self.style.WARNING(
                        f"Hoy (día ISO {hoy_iso}) no está configurado para enviar recordatorio. "
                        "Usa --fuerza para enviar de todas formas."
                    )
                )
                return

        sucursales = SucursalCliente.objects.filter(activa=True).order_by("nombre")
        if solo_sucursal:
            sucursales = sucursales.filter(nombre__iexact=solo_sucursal)
            if not sucursales.exists():
                raise CommandError(
                    f'No se encontró una sucursal/cliente activa con nombre "{solo_sucursal}".'
                )

        if not sucursales.exists():
            self.stdout.write(self.style.WARNING("No hay sucursales/clientes activos."))
            return

        remitente = config.email_remitente or settings.DEFAULT_FROM_EMAIL
        asunto = "Recordatorio: captura tu pedido de hoy - Los Tocayos"

        enviados = 0
        fallidos = 0
        saltados = 0

        for sucursal in sucursales:
            if not sucursal.email:
                saltados += 1
                LogRecordatorio.objects.create(
                    sucursal_cliente=sucursal,
                    estado=LogRecordatorio.Estado.SALTADO,
                    mensaje_error="Sin correo configurado.",
                )
                self.stdout.write(self.style.WARNING(f"Saltado: {sucursal.nombre} no tiene correo configurado."))
                continue

            contexto = {
                "sucursal": sucursal,
                "hora_fin_pedidos": config.hora_fin_pedidos,
                "hora_actual": timezone.localtime(),
                "contacto_correo": settings.REMINDER_CONTACT_EMAIL,
            }
            texto_plano = render_to_string("pedidos/emails/recordatorio.txt", contexto)
            html = render_to_string("pedidos/emails/recordatorio.html", contexto)

            if modo_test:
                self.stdout.write(
                    self.style.SUCCESS(f"[TEST] Se simuló el envío a {sucursal.nombre} <{sucursal.email}>.")
                )
                enviados += 1
                continue

            try:
                mensaje = EmailMultiAlternatives(asunto, texto_plano, remitente, [sucursal.email])
                mensaje.attach_alternative(html, "text/html")
                mensaje.send(fail_silently=False)
            except Exception as error:  # noqa: BLE001 - se registra y se continúa con las demás sucursales
                fallidos += 1
                LogRecordatorio.objects.create(
                    sucursal_cliente=sucursal,
                    estado=LogRecordatorio.Estado.ERROR,
                    mensaje_error=str(error),
                )
                logger.error("Error enviando recordatorio a %s <%s>: %s", sucursal.nombre, sucursal.email, error)
                self.stdout.write(self.style.ERROR(f"Error enviando a {sucursal.nombre}: {error}"))
            else:
                enviados += 1
                LogRecordatorio.objects.create(
                    sucursal_cliente=sucursal,
                    estado=LogRecordatorio.Estado.ENVIADO,
                )
                logger.info("Recordatorio enviado a %s <%s>.", sucursal.nombre, sucursal.email)

        resumen = f"Recordatorios: {enviados} enviados, {fallidos} fallidos, {saltados} saltados."
        self.stdout.write(self.style.SUCCESS(resumen) if fallidos == 0 else self.style.WARNING(resumen))
