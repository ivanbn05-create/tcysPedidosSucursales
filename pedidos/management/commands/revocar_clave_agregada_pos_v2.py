"""Revoke an aggregate recovery signing public key without deleting audit."""

import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from pedidos.models import PosRecoveryV2SigningKey


class Command(BaseCommand):
    help = "Revoca una clave pública recovery-v2; los ACK/recibos nuevos bajo ella fallan cerrado."

    def add_arguments(self, parser):
        parser.add_argument("--key-id", required=True)
        parser.add_argument("--referencia", required=True)
        parser.add_argument("--confirm", action="store_true")

    def handle(self, *args, **options):
        if not options["confirm"] or not 3 <= len(options["referencia"]) <= 120:
            raise CommandError("Se requiere --confirm y referencia auditada.")
        try:
            key_id = uuid.UUID(options["key_id"])
        except ValueError as exc:
            raise CommandError("key_id inválido.") from exc
        with transaction.atomic():
            key = PosRecoveryV2SigningKey.objects.select_for_update().filter(pk=key_id).first()
            if key is None:
                raise CommandError("Clave no registrada.")
            if key.revoked_at is not None or not key.active:
                self.stdout.write(f"Clave {key.pk} ya revocada.")
                return
            key.active = False
            key.revoked_at = timezone.now()
            key.revocation_reference = options["referencia"]
            key.save(update_fields=["active", "revoked_at", "revocation_reference"])
        self.stdout.write(f"Clave {key.pk} revocada; material público no impreso.")
