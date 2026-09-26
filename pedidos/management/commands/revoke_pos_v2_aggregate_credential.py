"""Revoke an exact-tuple POS bearer without printing or deleting it."""

import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from pedidos.models import PosAggregatedCredential


class Command(BaseCommand):
    help = "Revoca una credencial POS v2 agregada con referencia auditada."

    def add_arguments(self, parser):
        parser.add_argument("--credential-id", required=True)
        parser.add_argument("--referencia", required=True)
        parser.add_argument("--confirm", action="store_true")

    def handle(self, *args, **options):
        if not options["confirm"] or not 3 <= len(options["referencia"]) <= 120:
            raise CommandError("Se requiere --confirm y referencia auditada.")
        try:
            credential_id = uuid.UUID(options["credential_id"])
        except ValueError as exc:
            raise CommandError("credential_id inválido.") from exc
        with transaction.atomic():
            credential = PosAggregatedCredential.objects.select_for_update().filter(pk=credential_id).first()
            if credential is None:
                raise CommandError("Credencial no registrada.")
            if not credential.active or credential.revoked_at is not None:
                self.stdout.write(f"Credencial {credential.pk} ya revocada.")
                return
            credential.active = False
            credential.revoked_at = timezone.now()
            credential.revocation_reference = options["referencia"]
            credential.save(update_fields=["active", "revoked_at", "revocation_reference"])
        self.stdout.write(f"Credencial {credential.pk} revocada; bearer no impreso.")
