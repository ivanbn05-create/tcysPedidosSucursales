import hashlib
import os
import stat
import tempfile
import uuid
from io import StringIO
from pathlib import Path
from unittest import skipUnless

from django.core.management import call_command
from django.test import TestCase

from .models import PosApiCredential, SucursalCliente


@skipUnless(os.name == "posix", "Entrega de secretos 0600 probada sólo en Linux/POSIX")
class PosV2CredentialCommandTests(TestCase):
    def setUp(self):
        self.branch = SucursalCliente.objects.create(
            nombre="Sucursal laboratorio credencial",
            tipo=SucursalCliente.Tipo.SUCURSAL,
        )
        self.edge_id = uuid.uuid4()

    def test_emision_rotacion_y_revocacion_sin_imprimir_bearer(self):
        with tempfile.TemporaryDirectory(prefix="tcys-pos-v2-") as private_dir:
            os.chmod(private_dir, 0o700)
            first_file = Path(private_dir) / "primera.token"
            output = StringIO()
            call_command(
                "issue_pos_v2_credential", edge_id=str(self.edge_id),
                sucursal_id=self.branch.pk, output=str(first_file),
                reference="LAB-ISSUE-001", confirm=True, stdout=output,
            )
            first = PosApiCredential.objects.get()
            first_token = first_file.read_text(encoding="ascii").strip()
            self.assertEqual(stat.S_IMODE(first_file.stat().st_mode), 0o600)
            self.assertEqual(
                first.token_sha256, hashlib.sha256(first_token.encode("utf-8")).hexdigest()
            )
            self.assertEqual(first.scopes, ["orders:v2:read"])
            self.assertNotIn(first_token, output.getvalue())

            second_file = Path(private_dir) / "segunda.token"
            output = StringIO()
            call_command(
                "issue_pos_v2_credential", edge_id=str(self.edge_id),
                sucursal_id=self.branch.pk, output=str(second_file),
                reference="LAB-ROTATE-002", rotate_from=str(first.pk),
                confirm=True, stdout=output,
            )
            first.refresh_from_db()
            second = PosApiCredential.objects.exclude(pk=first.pk).get()
            self.assertFalse(first.active)
            self.assertIsNotNone(first.revoked_at)
            self.assertEqual(second.rotated_from_id, first.pk)
            self.assertNotIn(second_file.read_text(encoding="ascii").strip(), output.getvalue())

            call_command(
                "revoke_pos_v2_credential", credential_id=str(second.pk),
                reference="LAB-REVOKE-003", confirm=True, stdout=StringIO(),
            )
            second.refresh_from_db()
            self.assertFalse(second.active)
            self.assertIsNotNone(second.revoked_at)
