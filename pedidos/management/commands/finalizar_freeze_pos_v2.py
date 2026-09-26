"""Release a verified recovery barrier only with explicit audit reference."""

import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from pedidos.models import PosRecoveryV2, PosRecoveryV2Freeze
from pedidos.pos_recovery_v2 import _freeze


class Command(BaseCommand):
    help = "Libera freeze tras cierre; abortar pendientes deja el cursor sin avanzar."

    def add_arguments(self, parser):
        parser.add_argument("--freeze-id", required=True)
        parser.add_argument("--referencia", required=True)
        parser.add_argument("--abort-pending", action="store_true")
        parser.add_argument("--confirm", action="store_true")

    def handle(self, *args, **options):
        if not options["confirm"] or not 3 <= len(options["referencia"]) <= 120:
            raise CommandError("Se requiere --confirm y referencia auditada.")
        try:
            freeze_id = uuid.UUID(options["freeze_id"])
        except ValueError as exc:
            raise CommandError("UUID inválido.") from exc
        with transaction.atomic():
            freeze = PosRecoveryV2Freeze.objects.select_for_update().filter(pk=freeze_id).first()
            if freeze is None:
                raise CommandError("Freeze no registrado.")
            if not freeze.active:
                if freeze.end_reference == options["referencia"]:
                    self.stdout.write(f"Freeze {freeze.pk} ya liberado.")
                    return
                raise CommandError("Freeze ya liberado con otra referencia.")
            _freeze(freeze.pk, freeze.sender_ids, lock=True)
            pending = PosRecoveryV2.objects.filter(freeze=freeze, estado="pendiente")
            if pending.exists() and not options["abort_pending"]:
                raise CommandError("Hay recuperaciones pendientes; exige --abort-pending.")
            if pending.exists():
                pending.update(estado="intervencion_manual", closed_reference=options["referencia"])
            freeze.active = False
            freeze.ended_at = timezone.now()
            freeze.end_reference = options["referencia"]
            freeze.save(update_fields=["active", "ended_at", "end_reference"])
        self.stdout.write(f"Freeze {freeze.pk} liberado; nunca se avanzó cursor en Pedidos.")
