"""Revoke a recovery acknowledgement signing key without deleting audit history."""

import os
import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from pedidos.models import PosEdgeSigningKey
from pedidos.pos_recovery import operator_name


class Command(BaseCommand):
    help = "Revoca una clave Ed25519 de Edge sin borrar actas existentes."

    def add_arguments(self, parser):
        parser.add_argument("--key-id", required=True)
        parser.add_argument("--referencia", required=True)
        parser.add_argument("--confirm", action="store_true")

    def handle(self, *args, **options):
        if os.name != "posix" or not options["confirm"] or not 3 <= len(options["referencia"]) <= 120:
            raise CommandError("Se requiere operador POSIX, confirmación y referencia.")
        try:
            key_id = uuid.UUID(options["key_id"])
        except ValueError as exc:
            raise CommandError("key_id inválido.") from exc
        with transaction.atomic():
            key = PosEdgeSigningKey.objects.select_for_update().filter(pk=key_id).first()
            if key is None:
                raise CommandError("Clave no registrada.")
            if key.revoked_at is not None:
                if key.revocation_reference != options["referencia"]:
                    raise CommandError("Clave ya revocada con otra referencia.")
                self.stdout.write(f"Clave {key_id} ya revocada.")
                return
            key.active = False
            key.revoked_at = timezone.now()
            key.revocation_reference = options["referencia"]
            key.revoked_operator = operator_name()
            key.save(update_fields=["active", "revoked_at", "revocation_reference", "revoked_operator"])
        self.stdout.write(f"Clave {key_id} revocada; ningún acuse nuevo será aceptado.")
