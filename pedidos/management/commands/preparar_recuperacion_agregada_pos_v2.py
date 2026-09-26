"""Create one signed baseline for an exact Edge/sender tuple."""

import uuid

from django.core.management.base import BaseCommand, CommandError

from pedidos.pos_recovery_v2 import prepare


class Command(BaseCommand):
    help = "Prepara baseline firmada sin avanzar cursor POS; requiere freeze PostgreSQL activo."

    def add_arguments(self, parser):
        for name in ("recovery-id", "edge-id", "pos-branch-id", "freeze-id", "pedidos-key-id"):
            parser.add_argument(f"--{name}", required=True)
        for name in ("sender-ids", "desde", "hasta", "prestate-file", "private-key-file",
                     "archive-dir",
                     "archivo", "referencia"):
            parser.add_argument(f"--{name}", required=True)
        parser.add_argument("--confirm", action="store_true")

    def handle(self, *args, **options):
        if not options["confirm"]:
            raise CommandError("Se requiere --confirm.")
        try:
            ids = {key: uuid.UUID(options[key]) for key in (
                "recovery_id", "edge_id", "pos_branch_id", "freeze_id", "pedidos_key_id"
            )}
            sender_ids = [int(v) for v in options["sender_ids"].split(",")]
        except ValueError as exc:
            raise CommandError("UUID/IDs inválidos.") from exc
        record = prepare(
            **ids, sender_ids=sender_ids, desde=options["desde"], hasta=options["hasta"],
            prestate_file=options["prestate_file"], private_key_file=options["private_key_file"],
            output=options["archivo"], archive_dir=options["archive_dir"],
            reference=options["referencia"],
        )
        self.stdout.write(
            f"Recovery {record.pk} pendiente: snapshot_sha256={record.snapshot_sha256}; "
            f"manifest_sha256={record.manifest_sha256}; pedidos={record.orders_count}; "
            f"tombstones={record.tombstones_count}. Cursor intacto."
        )
