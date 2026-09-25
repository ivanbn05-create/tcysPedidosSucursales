import hashlib
import json
import os
import stat
import tempfile
import uuid
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest import skipUnless

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from .models import (
    Pedido, PedidoPurgado, PosApiCredential, PosRetentionRecovery,
    RegistroPurga, SucursalCliente,
)
from .pos_recovery import _snapshot, _uuid_set, complete, prepare


class RecoveryValidationTests(TestCase):
    def test_rechaza_identidades_duplicadas_y_malformadas(self):
        value = str(uuid.uuid4())
        with self.assertRaises(CommandError):
            _uuid_set([value, value])
        with self.assertRaises(CommandError):
            _uuid_set(["no-es-uuid"])

    def test_purga_real_no_acepta_un_flag_simple(self):
        with self.assertRaises(CommandError):
            call_command("purgar_retencion", apply=True, stdout=StringIO())

    def test_baseline_no_mezcla_sucursales_y_incluye_purga_fuera_de_ventana(self):
        one = SucursalCliente.objects.create(nombre="Arboledas sintética", tipo="sucursal")
        other = SucursalCliente.objects.create(nombre="Otra sintética", tipo="sucursal")
        now = timezone.now()
        valid = Pedido.objects.create(
            sucursal_cliente=one, usuario_nombre="fixture",
            estado=Pedido.Estado.CONFIRMADO, fecha_confirmacion=now,
        )
        Pedido.objects.create(
            sucursal_cliente=other, usuario_nombre="fixture",
            estado=Pedido.Estado.CONFIRMADO, fecha_confirmacion=now,
        )
        receipt = RegistroPurga.objects.create(
            motivo="antiguedad", numero_pedidos=2, numero_items=0,
            numero_macropedidos=0,
        )
        tombstone_one = uuid.uuid4()
        tombstone_other = uuid.uuid4()
        for branch, key, origin in ((one, tombstone_one, 9001), (other, tombstone_other, 9002)):
            PedidoPurgado.objects.create(
                codigo_publico=key, pedido_id_origen=origin,
                sucursal_cliente_id=branch.pk,
                fecha_confirmacion=now - timedelta(days=2),
                motivo="antiguedad", registro=receipt,
            )
        rows, tombstones = _snapshot(one.pk, now - timedelta(hours=1), now + timedelta(hours=1))
        self.assertEqual([row["codigo_publico"] for row in rows], [str(valid.codigo_publico)])
        self.assertEqual(tombstones, [str(tombstone_one)])


@skipUnless(os.name == "posix", "Artefactos privados 0600 requieren Linux/POSIX")
class RecoveryCommandTests(TestCase):
    def setUp(self):
        self.branch = SucursalCliente.objects.create(
            nombre="Arboledas sintética", tipo=SucursalCliente.Tipo.SUCURSAL,
        )
        self.edge_id = uuid.uuid4()
        self.pos_branch_id = uuid.uuid4()
        PosApiCredential.objects.create(
            edge_id=self.edge_id, pos_branch_id=self.pos_branch_id,
            sucursal_cliente=self.branch,
            token_sha256=hashlib.sha256(b"credencial-sintetica-recovery").hexdigest(),
            scopes=["orders:v2:read"],
        )
        self.desde = timezone.now() - timedelta(hours=2)
        self.hasta = timezone.now() + timedelta(hours=2)
        self.order = Pedido.objects.create(
            sucursal_cliente=self.branch, usuario_nombre="Fixture sintético",
            estado=Pedido.Estado.CONFIRMADO, fecha_confirmacion=timezone.now(),
        )
        self.purged_id = uuid.uuid4()
        receipt = RegistroPurga.objects.create(
            motivo="exportacion", numero_pedidos=1, numero_items=0,
            numero_macropedidos=0,
        )
        PedidoPurgado.objects.create(
            codigo_publico=self.purged_id, pedido_id_origen=9999,
            sucursal_cliente_id=self.branch.pk, fecha_confirmacion=timezone.now(),
            motivo="exportacion", registro=receipt,
        )
        self.recovery_id = uuid.uuid4()

    def _write_private(self, path, value):
        path.write_bytes(value)
        os.chmod(path, 0o600)
        return hashlib.sha256(value).hexdigest()

    def test_baseline_idempotente_y_cierre_solo_con_cobertura_total(self):
        with tempfile.TemporaryDirectory(prefix="tcys-recovery-") as root:
            os.chmod(root, 0o700)
            root = Path(root)
            cursor_file = root / "cursor.txt"
            self._write_private(cursor_file, b"")
            snapshot = root / "baseline.zip"
            args = dict(
                recovery_id=self.recovery_id, edge_id=self.edge_id,
                pos_branch_id=self.pos_branch_id, branch_id=self.branch.pk,
                desde=self.desde.isoformat(), hasta=self.hasta.isoformat(),
                old_cursor_file=cursor_file, output=snapshot, reference="LAB-REC-001",
            )
            record = prepare(**args)
            self.assertEqual(record.orders_count, 1)
            self.assertEqual(record.tombstones_count, 1)
            self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o600)
            self.assertEqual(prepare(**args).pk, record.pk)
            self.assertEqual(PosRetentionRecovery.objects.count(), 1)

            ack = root / "ack.json"
            custody = root / "custody.json"
            ack_body = {
                "recovery_id": str(record.pk), "edge_id": str(self.edge_id),
                "pos_branch_id": str(self.pos_branch_id),
                "sucursal_cliente_id": self.branch.pk,
                "snapshot_sha256": record.snapshot_sha256,
                "orders_sha256": record.orders_sha256,
                "old_cursor_sha256": record.old_cursor_sha256,
                "received_order_ids": [str(self.order.codigo_publico)],
                "unresolved_order_ids": [str(self.purged_id)],
            }
            custody_body = {
                "recovery_id": str(record.pk),
                "restored_from_archive_ids": [], "already_on_edge_ids": [],
            }
            ack_hash = self._write_private(ack, json.dumps(ack_body).encode())
            custody_hash = self._write_private(custody, json.dumps(custody_body).encode())
            close_args = dict(
                recovery_id=self.recovery_id, snapshot_file=snapshot,
                snapshot_sha256=record.snapshot_sha256, edge_ack_file=ack,
                edge_ack_sha256=ack_hash, custody_file=custody,
                custody_sha256=custody_hash, freeze_evidence_sha256="a" * 64,
                reference="LAB-CLOSE-001",
            )
            with self.assertRaises(CommandError):
                complete(**{**close_args, "snapshot_sha256": "0" * 64})
            self.assertEqual(PosRetentionRecovery.objects.get(pk=record.pk).estado,
                             PosRetentionRecovery.Estado.PENDIENTE)
            manual = complete(**close_args)
            self.assertEqual(manual.estado, PosRetentionRecovery.Estado.MANUAL)
            self.assertIsNone(manual.cerrada_en)

            ack_body["received_order_ids"].append(str(self.purged_id))
            ack_body["unresolved_order_ids"] = []
            custody_body["restored_from_archive_ids"] = [str(self.purged_id)]
            close_args["edge_ack_sha256"] = self._write_private(ack, json.dumps(ack_body).encode())
            close_args["custody_sha256"] = self._write_private(custody, json.dumps(custody_body).encode())
            done = complete(**close_args)
            self.assertEqual(done.estado, PosRetentionRecovery.Estado.COMPLETADA)
            self.assertEqual(complete(**close_args).pk, done.pk)

    def test_no_abre_recuperacion_sin_410(self):
        PedidoPurgado.objects.all().delete()
        RegistroPurga.objects.all().delete()
        with tempfile.TemporaryDirectory(prefix="tcys-recovery-") as root:
            os.chmod(root, 0o700)
            root = Path(root)
            cursor_file = root / "cursor.txt"
            self._write_private(cursor_file, b"")
            with self.assertRaises(CommandError):
                prepare(
                    recovery_id=self.recovery_id, edge_id=self.edge_id,
                    pos_branch_id=self.pos_branch_id, branch_id=self.branch.pk,
                    desde=self.desde.isoformat(), hasta=self.hasta.isoformat(),
                    old_cursor_file=cursor_file, output=root / "baseline.zip",
                    reference="LAB-REC-002",
                )
            self.assertEqual(PosRetentionRecovery.objects.count(), 0)

    def test_cursor_obsoleto_incluye_purga_fuera_de_ventana(self):
        PedidoPurgado.objects.filter(pk=self.purged_id).update(
            fecha_confirmacion=self.desde - timedelta(days=1)
        )
        with tempfile.TemporaryDirectory(prefix="tcys-recovery-") as root:
            os.chmod(root, 0o700)
            root = Path(root)
            cursor_file = root / "cursor.txt"
            self._write_private(cursor_file, b"1727034600123456")
            record = prepare(
                recovery_id=self.recovery_id, edge_id=self.edge_id,
                pos_branch_id=self.pos_branch_id, branch_id=self.branch.pk,
                desde=self.desde.isoformat(), hasta=self.hasta.isoformat(),
                old_cursor_file=cursor_file, output=root / "baseline.zip",
                reference="LAB-REC-003",
            )
            self.assertEqual(record.orders_count, 1)
            self.assertEqual(record.tombstones_count, 1)
