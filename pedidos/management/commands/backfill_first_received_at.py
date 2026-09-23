from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.dateparse import parse_datetime

from pedidos.models import EventoCliente, Pedido


class Command(BaseCommand):
    help = "Propone o aplica fecha de primera recepción respaldada por evidencia de migración."

    def add_arguments(self, parser):
        parser.add_argument("--fecha-recepcion", required=True)
        parser.add_argument("--evidencia", required=True)
        parser.add_argument("--modelo", choices=("pedido", "evento", "ambos"), default="ambos")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **opciones):
        fecha = parse_datetime(opciones["fecha_recepcion"])
        if fecha is None or fecha.tzinfo is None:
            raise CommandError("fecha-recepcion requiere ISO 8601 con zona horaria.")
        if not opciones["evidencia"].strip():
            raise CommandError("Se requiere referencia de evidencia del primer ingreso al VPS.")
        modelos = {"pedido": Pedido, "evento": EventoCliente}
        elegidos = modelos if opciones["modelo"] == "ambos" else {
            opciones["modelo"]: modelos[opciones["modelo"]]
        }
        conteos = {
            nombre: modelo.objects.filter(first_received_at__isnull=True).count()
            for nombre, modelo in elegidos.items()
        }
        if opciones["apply"]:
            if not getattr(settings, "RETENTION_BACKFILL_ENABLED", False):
                raise CommandError("Backfill real deshabilitado por configuración.")
            with transaction.atomic():
                conteos = {
                    nombre: modelo.objects.filter(first_received_at__isnull=True).update(
                        first_received_at=fecha
                    )
                    for nombre, modelo in elegidos.items()
                }
        self.stdout.write(
            f"modo={'real' if opciones['apply'] else 'dry-run'} "
            f"filas_pedido={conteos.get('pedido', 0)} "
            f"filas_evento={conteos.get('evento', 0)} "
            f"evidencia={opciones['evidencia']}"
        )
