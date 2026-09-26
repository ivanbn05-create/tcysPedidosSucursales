"""Start PostgreSQL-enforced freeze for one aggregate recovery."""

import uuid

from django.core.management.base import BaseCommand, CommandError

from pedidos.pos_recovery_v2 import start_freeze


class Command(BaseCommand):
    help = "Activa barrera PostgreSQL de escritores para una tupla exacta de remitentes."

    def add_arguments(self, parser):
        parser.add_argument("--freeze-id", required=True)
        parser.add_argument("--sender-ids", required=True)
        parser.add_argument("--referencia", required=True)
        parser.add_argument("--confirm", action="store_true")

    def handle(self, *args, **options):
        if not options["confirm"]:
            raise CommandError("Se requiere --confirm.")
        try:
            freeze_id = uuid.UUID(options["freeze_id"])
            sender_ids = [int(v) for v in options["sender_ids"].split(",")]
        except ValueError as exc:
            raise CommandError("UUID/IDs inválidos.") from exc
        freeze = start_freeze(
            freeze_id=freeze_id, sender_ids=sender_ids, reference=options["referencia"]
        )
        self.stdout.write(f"Freeze {freeze.pk} activo; remitentes={freeze.sender_ids}.")
