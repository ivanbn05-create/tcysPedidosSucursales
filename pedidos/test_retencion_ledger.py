"""Ledger externo mínimo para impedir reintroducción tras un restore antiguo."""

import io
import json
import os
import stat
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import (
    ExportacionRetencion,
    Pedido,
    PedidoPurgado,
    RegistroPurga,
    SucursalCliente,
)
from .retencion import conciliar_restauracion
from .retencion_ledger import LedgerError, exportar_ledger, importar_ledger


class LedgerRetencionTests(TestCase):
    def setUp(self):
        temporal = tempfile.TemporaryDirectory()
        self.addCleanup(temporal.cleanup)
        self.ruta = Path(temporal.name) / "ledger.jsonl"
        ahora = timezone.now().replace(microsecond=0)
        self.exportacion = ExportacionRetencion.objects.create(
            estado=ExportacionRetencion.Estado.CONFIRMADA,
            confirmada_en=ahora,
            archivo_local_eliminado_en=ahora,
            desde_recepcion=ahora - timedelta(days=2),
            hasta_recepcion=ahora - timedelta(days=1),
            sha256_archivo="a" * 64,
            sha256_contenido="b" * 64,
            archivo_local="/ruta/privada/no-exportar.zip",
            numero_pedidos=1,
            numero_items=2,
            referencia_confirmacion="Referencia privada no exportar",
        )
        self.registro = RegistroPurga.objects.create(
            motivo="exportacion",
            numero_pedidos=1,
            numero_items=2,
            numero_macropedidos=0,
            numero_eventos=0,
            fecha_confirmacion_min=ahora - timedelta(days=2),
            fecha_confirmacion_max=ahora - timedelta(days=2),
            exportacion=self.exportacion,
        )
        self.codigo = uuid.uuid4()
        self.fecha_confirmacion = ahora - timedelta(days=2)
        PedidoPurgado.objects.create(
            codigo_publico=self.codigo,
            pedido_id_origen=42,
            sucursal_cliente_id=7,
            fecha_confirmacion=self.fecha_confirmacion,
            motivo="exportacion",
            exportacion=self.exportacion,
            registro=self.registro,
        )

    def test_exportacion_minima_sha_0600_y_sin_overwrite(self):
        resultado = exportar_ledger(self.ruta)
        texto = self.ruta.read_text(encoding="utf-8")
        self.assertEqual(resultado["recibos"], 1)
        self.assertEqual(resultado["tombstones"], 1)
        self.assertEqual(resultado["exportaciones"], 1)
        filas = [json.loads(linea) for linea in texto.splitlines()]
        self.assertEqual(filas[0]["version"], 2)
        tombstone = next(fila for fila in filas if fila["kind"] == "tombstone")
        self.assertEqual(tombstone["sucursal_cliente_id"], 7)
        self.assertEqual(
            tombstone["fecha_confirmacion"],
            self.fecha_confirmacion.isoformat(timespec="microseconds").replace(
                "+00:00", "Z"
            ),
        )
        self.assertEqual(Path(f"{self.ruta}.sha256").read_text().strip(), resultado["sha256"])
        self.assertNotIn("Referencia privada", texto)
        self.assertNotIn("no-exportar.zip", texto)
        self.assertNotIn('"archivo_local":', texto)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.ruta.stat().st_mode), 0o600)
        with self.assertRaises(LedgerError):
            exportar_ledger(self.ruta)

    @override_settings(RETENTION_RESTORE_ISOLATED=True)
    def test_snapshot_anterior_importacion_idempotente_y_pedido_reintroducido(self):
        resultado = exportar_ledger(self.ruta)
        PedidoPurgado.objects.all().delete()
        RegistroPurga.objects.all().delete()
        ExportacionRetencion.objects.all().delete()

        sucursal = SucursalCliente.objects.create(
            nombre="Sucursal de ensayo", tipo=SucursalCliente.Tipo.SUCURSAL
        )
        Pedido.objects.create(
            id=42,
            codigo_publico=self.codigo,
            sucursal_cliente=sucursal,
            usuario_nombre="Nombre que no debe aparecer en ledger",
            estado=Pedido.Estado.RECIBIDO,
        )

        resumen = importar_ledger(
            self.ruta, expected_sha256=resultado["sha256"], aplicar=True
        )
        self.assertTrue(resumen["requiere_conciliacion"])
        self.assertEqual(resumen["pedidos_reintroducidos"], 1)
        self.assertEqual(RegistroPurga.objects.get().pk, self.registro.pk)
        self.assertEqual(PedidoPurgado.objects.get().codigo_publico, self.codigo)
        self.assertEqual(PedidoPurgado.objects.get().exportacion_id, self.exportacion.pk)
        self.assertEqual(PedidoPurgado.objects.get().sucursal_cliente_id, 7)
        self.assertEqual(
            PedidoPurgado.objects.get().fecha_confirmacion,
            self.fecha_confirmacion,
        )
        self.assertEqual(ExportacionRetencion.objects.get().archivo_local, "")
        self.assertEqual(ExportacionRetencion.objects.get().referencia_confirmacion, "")
        self.assertEqual(conciliar_restauracion(aplicar=False)["pedidos_reintroducidos"], 1)

        segundo = importar_ledger(
            self.ruta, expected_sha256=resultado["sha256"], aplicar=True
        )
        self.assertEqual(segundo, resumen)
        self.assertEqual(RegistroPurga.objects.count(), 1)
        self.assertEqual(PedidoPurgado.objects.count(), 1)

    def test_importacion_exige_aislamiento_apply_y_sha(self):
        resultado = exportar_ledger(self.ruta)
        with self.assertRaisesRegex(LedgerError, "RETENTION_RESTORE_ISOLATED"):
            importar_ledger(
                self.ruta, expected_sha256=resultado["sha256"], aplicar=True
            )
        with override_settings(RETENTION_RESTORE_ISOLATED=True):
            with self.assertRaisesRegex(LedgerError, "--apply"):
                importar_ledger(self.ruta, expected_sha256=resultado["sha256"])
            with self.assertRaisesRegex(LedgerError, "SHA-256"):
                importar_ledger(self.ruta, expected_sha256="0" * 64, aplicar=True)

    @override_settings(RETENTION_RESTORE_ISOLATED=True)
    def test_conflicto_revierte_toda_la_importacion(self):
        resultado = exportar_ledger(self.ruta)
        PedidoPurgado.objects.all().delete()
        RegistroPurga.objects.filter(pk=self.registro.pk).update(motivo="otro")

        with self.assertRaisesRegex(LedgerError, "Conflicto"):
            importar_ledger(
                self.ruta, expected_sha256=resultado["sha256"], aplicar=True
            )
        self.assertEqual(PedidoPurgado.objects.count(), 0)
        self.assertEqual(RegistroPurga.objects.get().motivo, "otro")

    @override_settings(RETENTION_RESTORE_ISOLATED=True)
    def test_conflicto_en_sucursal_o_fecha_del_tombstone_se_rechaza(self):
        resultado = exportar_ledger(self.ruta)
        PedidoPurgado.objects.filter(pk=self.codigo).update(sucursal_cliente_id=8)
        with self.assertRaisesRegex(LedgerError, "Conflicto con tombstone"):
            importar_ledger(
                self.ruta, expected_sha256=resultado["sha256"], aplicar=True
            )
        PedidoPurgado.objects.filter(pk=self.codigo).update(
            sucursal_cliente_id=7,
            fecha_confirmacion=self.fecha_confirmacion + timedelta(minutes=1),
        )
        with self.assertRaisesRegex(LedgerError, "Conflicto con tombstone"):
            importar_ledger(
                self.ruta, expected_sha256=resultado["sha256"], aplicar=True
            )

    @override_settings(RETENTION_RESTORE_ISOLATED=True)
    def test_fecha_confirmacion_nula_se_preserva(self):
        PedidoPurgado.objects.filter(pk=self.codigo).update(fecha_confirmacion=None)
        resultado = exportar_ledger(self.ruta)
        PedidoPurgado.objects.all().delete()
        RegistroPurga.objects.all().delete()
        importar_ledger(self.ruta, expected_sha256=resultado["sha256"], aplicar=True)
        self.assertIsNone(PedidoPurgado.objects.get().fecha_confirmacion)

    @override_settings(RETENTION_RESTORE_ISOLATED=True)
    def test_snapshot_con_lote_generado_avanza_a_confirmado(self):
        resultado = exportar_ledger(self.ruta)
        PedidoPurgado.objects.all().delete()
        RegistroPurga.objects.all().delete()
        ExportacionRetencion.objects.filter(pk=self.exportacion.pk).update(
            estado=ExportacionRetencion.Estado.GENERADA,
            confirmada_en=None,
            archivo_local_eliminado_en=None,
            archivo_local="/ruta/privada/zip-antiguo.zip",
        )

        importar_ledger(self.ruta, expected_sha256=resultado["sha256"], aplicar=True)
        lote = ExportacionRetencion.objects.get(pk=self.exportacion.pk)
        self.assertEqual(lote.estado, ExportacionRetencion.Estado.CONFIRMADA)
        self.assertEqual(lote.confirmada_en, self.exportacion.confirmada_en)
        self.assertEqual(
            lote.archivo_local_eliminado_en,
            self.exportacion.archivo_local_eliminado_en,
        )
        self.assertEqual(lote.archivo_local, "")
        self.assertEqual(PedidoPurgado.objects.count(), 1)
        importar_ledger(self.ruta, expected_sha256=resultado["sha256"], aplicar=True)

    @override_settings(RETENTION_RESTORE_ISOLATED=True)
    def test_lote_generado_con_hash_distinto_no_avanza(self):
        resultado = exportar_ledger(self.ruta)
        PedidoPurgado.objects.all().delete()
        RegistroPurga.objects.all().delete()
        ExportacionRetencion.objects.filter(pk=self.exportacion.pk).update(
            estado=ExportacionRetencion.Estado.GENERADA,
            confirmada_en=None,
            archivo_local_eliminado_en=None,
            sha256_archivo="c" * 64,
        )
        with self.assertRaisesRegex(LedgerError, "Conflicto"):
            importar_ledger(
                self.ruta, expected_sha256=resultado["sha256"], aplicar=True
            )
        self.assertFalse(PedidoPurgado.objects.exists())

    @override_settings(RETENTION_RESTORE_ISOLATED=True)
    def test_id_original_ocupado_por_otro_pedido_falla_cerrado(self):
        resultado = exportar_ledger(self.ruta)
        PedidoPurgado.objects.all().delete()
        RegistroPurga.objects.all().delete()
        sucursal = SucursalCliente.objects.create(
            nombre="Sucursal conflicto", tipo=SucursalCliente.Tipo.SUCURSAL
        )
        Pedido.objects.create(
            id=42, codigo_publico=uuid.uuid4(), sucursal_cliente=sucursal,
            usuario_nombre="Prueba", estado=Pedido.Estado.RECIBIDO,
        )

        with self.assertRaisesRegex(LedgerError, "otro pedido"):
            importar_ledger(
                self.ruta, expected_sha256=resultado["sha256"], aplicar=True
            )
        self.assertFalse(RegistroPurga.objects.exists())
        self.assertFalse(PedidoPurgado.objects.exists())

    def test_comandos_no_imprimen_filas_y_requieren_apply(self):
        salida = io.StringIO()
        call_command("exportar_ledger_retencion", destino=str(self.ruta), stdout=salida)
        self.assertNotIn(str(self.codigo), salida.getvalue())
        sha = Path(f"{self.ruta}.sha256").read_text().strip()
        with override_settings(RETENTION_RESTORE_ISOLATED=True):
            with self.assertRaises(CommandError):
                call_command(
                    "importar_ledger_retencion",
                    origen=str(self.ruta),
                    sha256_esperado=sha,
                    stdout=io.StringIO(),
                )
