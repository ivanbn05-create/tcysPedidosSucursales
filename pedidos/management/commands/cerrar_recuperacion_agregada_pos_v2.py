"""Verify one aggregate recovery and issue Pedidos-signed receipt."""

import uuid

from django.core.management.base import BaseCommand, CommandError

from pedidos.pos_recovery_v2 import complete


class Command(BaseCommand):
    help = "Cierra todo o emite acta manual; nunca avanza cursor en Pedidos."

    def add_arguments(self, parser):
        parser.add_argument("--recovery-id", required=True)
        for name in ("snapshot-file", "snapshot-sha256", "edge-ack-file", "edge-ack-sha256",
                     "archive-dir", "private-key-file", "receipt-file", "referencia"):
            parser.add_argument(f"--{name}", required=True)
        parser.add_argument("--confirm", action="store_true")

    def handle(self, *args, **options):
        if not options["confirm"]:
            raise CommandError("Se requiere --confirm.")
        try:
            recovery_id = uuid.UUID(options["recovery_id"])
        except ValueError as exc:
            raise CommandError("UUID inválido.") from exc
        record = complete(
            recovery_id=recovery_id, snapshot_file=options["snapshot_file"],
            snapshot_sha256=options["snapshot_sha256"], edge_ack_file=options["edge_ack_file"],
            edge_ack_sha256=options["edge_ack_sha256"], archive_dir=options["archive_dir"],
            private_key_file=options["private_key_file"], receipt_file=options["receipt_file"],
            reference=options["referencia"],
        )
        self.stdout.write(
            f"Recovery {record.pk}: estado={record.estado}; recibo_sha256="
            f"{record.receipt_sha256}. Sólo recibo completada permite checkpoint POS."
        )
