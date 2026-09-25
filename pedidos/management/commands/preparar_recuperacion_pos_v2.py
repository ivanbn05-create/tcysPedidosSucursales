"""Genera una baseline privada tras reproducir un 410 para un Edge concreto."""

import uuid

from django.core.management.base import BaseCommand, CommandError

from pedidos.pos_recovery import prepare


class Command(BaseCommand):
    help = "Prepara snapshot privado para conciliación POS v2; no avanza cursores."

    def add_arguments(self, parser):
        parser.add_argument("--recovery-id", required=True)
        parser.add_argument("--edge-id", required=True)
        parser.add_argument("--pos-branch-id", required=True)
        parser.add_argument("--sucursal-id", required=True, type=int)
        parser.add_argument("--desde", required=True)
        parser.add_argument("--hasta", required=True)
        parser.add_argument("--cursor-file", required=True)
        parser.add_argument("--archivo", required=True)
        parser.add_argument("--referencia", required=True)
        parser.add_argument("--writers-frozen", action="store_true")
        parser.add_argument("--confirm", action="store_true")

    def handle(self, *args, **options):
        if not options["confirm"] or not options["writers_frozen"]:
            raise CommandError("Se requiere confirmación explícita y freeze verificado de escritores/purga.")
        try:
            ids = {name: uuid.UUID(options[name]) for name in
                   ("recovery_id", "edge_id", "pos_branch_id")}
        except ValueError as exc:
            raise CommandError("UUID de recuperación, Edge o sucursal POS inválido.") from exc
        record = prepare(
            recovery_id=ids["recovery_id"], edge_id=ids["edge_id"],
            pos_branch_id=ids["pos_branch_id"], branch_id=options["sucursal_id"],
            desde=options["desde"], hasta=options["hasta"],
            old_cursor_file=options["cursor_file"], output=options["archivo"],
            reference=options["referencia"],
        )
        self.stdout.write(
            f"Recuperación {record.pk}: estado={record.estado}; "
            f"snapshot_sha256={record.snapshot_sha256}; pedidos={record.orders_count}; "
            f"tombstones={record.tombstones_count}. No avanzar cursor todavía."
        )
