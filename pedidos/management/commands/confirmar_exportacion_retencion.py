import logging

from django.core.management.base import BaseCommand, CommandError

from pedidos.models import ExportacionRetencion
from pedidos.retencion import confirmar_exportacion
from pedidos.pos_recovery import operator_name


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Confirma el hash comprobado en destino y elimina el ZIP local del VPS."

    def add_arguments(self, parser):
        parser.add_argument("lote_id")
        parser.add_argument("--archivo-local", required=True)
        parser.add_argument("--sha256-destino", required=True)
        parser.add_argument("--referencia", required=True)
        parser.add_argument("--integridad-confirmada", action="store_true")

    def handle(self, *args, **opciones):
        if not opciones["integridad_confirmada"]:
            raise CommandError("Falta --integridad-confirmada tras verificar el hash en destino.")
        try:
            lote = confirmar_exportacion(
                lote_id=opciones["lote_id"],
                archivo_local=opciones["archivo_local"],
                sha256_destino=opciones["sha256_destino"],
                referencia=opciones["referencia"],
            )
        except (ExportacionRetencion.DoesNotExist, OSError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"lote={lote.pk} estado={lote.estado} archivo_local_eliminado=si")
        logger.info("retencion_confirm operacion=%s operador_os=%s lote=%s estado=%s",
                    opciones["referencia"], operator_name(), lote.pk, lote.estado)
