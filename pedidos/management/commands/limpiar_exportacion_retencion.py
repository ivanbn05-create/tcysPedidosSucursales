from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from pedidos.models import ExportacionRetencion
from pedidos.retencion import finalizar_limpieza_exportacion, invalidar_exportacion_obsoleta


class Command(BaseCommand):
    help = "Inspecciona o limpia manualmente un ZIP confirmado/inválido sin activar purga."

    def add_arguments(self, parser):
        parser.add_argument("lote_id")
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--permitir-ausente", action="store_true")

    def handle(self, *args, **opciones):
        if opciones["apply"] and not getattr(settings, "RETENTION_EXPORT_CLEANUP_ENABLED", False):
            raise CommandError("La limpieza real de exports está deshabilitada.")
        try:
            lote = ExportacionRetencion.objects.get(pk=opciones["lote_id"])
            if lote.estado == ExportacionRetencion.Estado.CONFIRMADA:
                if opciones["apply"]:
                    lote = finalizar_limpieza_exportacion(
                        lote_id=lote.pk, permitir_ausente=opciones["permitir_ausente"]
                    )
            else:
                lote = invalidar_exportacion_obsoleta(
                    lote_id=lote.pk,
                    aplicar=opciones["apply"],
                    permitir_ausente=opciones["permitir_ausente"],
                )
        except (ExportacionRetencion.DoesNotExist, OSError, RuntimeError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            f"lote={lote.pk} modo={'real' if opciones['apply'] else 'dry-run'} "
            f"estado={lote.estado} archivo_local_eliminado={'si' if lote.archivo_local_eliminado_en else 'no'}"
        )
