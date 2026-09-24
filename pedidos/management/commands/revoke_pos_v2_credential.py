"""Revoca una credencial POS v2 por su identificador público."""

import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from pedidos.models import PosApiCredential


class Command(BaseCommand):
    help = "Revoca una credencial POS v2, sin conocer ni mostrar su bearer."

    def add_arguments(self, parser):
        parser.add_argument("--credential-id", required=True)
        parser.add_argument("--reference", required=True)
        parser.add_argument("--confirm", action="store_true")

    def handle(self, *args, **options):
        if not options["confirm"]:
            raise CommandError("Se requiere --confirm.")
        reference = options["reference"].strip()
        if not 3 <= len(reference) <= 120:
            raise CommandError("--reference debe tener entre 3 y 120 caracteres.")
        try:
            credential_id = uuid.UUID(options["credential_id"])
        except ValueError as exc:
            raise CommandError("--credential-id debe ser un UUID válido.") from exc
        with transaction.atomic():
            credential = PosApiCredential.objects.select_for_update().filter(
                pk=credential_id
            ).first()
            if credential is None:
                raise CommandError("Credencial no encontrada.")
            if credential.active or credential.revoked_at is None:
                credential.active = False
                credential.revoked_at = timezone.now()
                credential.revocation_reference = reference
                credential.save(update_fields=["active", "revoked_at", "revocation_reference"])
        self.stdout.write(f"Credencial {credential_id} revocada.")
