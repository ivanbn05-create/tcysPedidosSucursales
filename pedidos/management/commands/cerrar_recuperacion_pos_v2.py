"""Cierra sólo una recuperación con acuse Edge y custodia cotejados."""

import uuid

from django.core.management.base import BaseCommand, CommandError

from pedidos.models import PosRetentionRecovery
from pedidos.pos_recovery import complete


class Command(BaseCommand):
    help = "Coteja baseline, acuse Edge y custodia; no cierra si falta un UUID."

    def add_arguments(self, parser):
        parser.add_argument("--recovery-id", required=True)
        parser.add_argument("--snapshot-file", required=True)
        parser.add_argument("--snapshot-sha256", required=True)
        parser.add_argument("--edge-ack-file", required=True)
        parser.add_argument("--edge-ack-sha256", required=True)
        parser.add_argument("--custody-file", required=True)
        parser.add_argument("--custody-sha256", required=True)
        parser.add_argument("--freeze-evidence-sha256", required=True)
        parser.add_argument("--referencia", required=True)
        parser.add_argument("--writers-frozen", action="store_true")
        parser.add_argument("--confirm", action="store_true")

    def handle(self, *args, **options):
        if not options["confirm"] or not options["writers_frozen"]:
            raise CommandError("Se requiere confirmación explícita y freeze vigente.")
        try:
            recovery_id = uuid.UUID(options["recovery_id"])
        except ValueError as exc:
            raise CommandError("UUID de recuperación inválido.") from exc
        record = complete(
            recovery_id=recovery_id, snapshot_file=options["snapshot_file"],
            snapshot_sha256=options["snapshot_sha256"],
            edge_ack_file=options["edge_ack_file"], edge_ack_sha256=options["edge_ack_sha256"],
            custody_file=options["custody_file"], custody_sha256=options["custody_sha256"],
            freeze_evidence_sha256=options["freeze_evidence_sha256"],
            reference=options["referencia"],
        )
        if record.estado != PosRetentionRecovery.Estado.COMPLETADA:
            raise CommandError(
                f"Recuperación {record.pk} requiere intervención manual; cursor anterior intacto."
            )
        self.stdout.write(
            f"Recuperación {record.pk} completada; siguiente ventana desde="
            f"{record.hasta.isoformat()}, cursor nuevo vacío, sólo tras registrar el ACK Edge."
        )
