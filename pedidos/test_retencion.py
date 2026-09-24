import tempfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib import admin
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import (
    ExportacionRetencion,
    ItemPedido,
    MacroPedido,
    Pedido,
    PedidoPurgado,
    Precio,
    Producto,
    RegistroPurga,
    SucursalCliente,
    Configuracion,
    EventoCliente,
)
from .retencion import (
    aplicar_purga,
    conciliar_restauracion,
    confirmar_exportacion,
    generar_exportacion,
    invalidar_exportacion_obsoleta,
    planificar_purga,
)
from .views import ocultar_referencia_pedido_purgado


class RetencionTests(TestCase):
    def setUp(self):
        self.ahora = timezone.now().replace(microsecond=0)
        self.sucursal = SucursalCliente.objects.create(
            nombre="Sucursal Retención", tipo=SucursalCliente.Tipo.SUCURSAL
        )
        self.producto = Producto.objects.create(nombre="Producto Retención")
        Precio.objects.create(
            producto=self.producto,
            sucursal_cliente=self.sucursal,
            precio_unitario=Decimal("10.00"),
        )
        self.configuracion = Configuracion.get_solo()

    def marcar_recepcion_historica(self, instancia, instante):
        """Prepara un dato histórico en la base de prueba sin quitar el guard real.

        El trigger de PostgreSQL fija la recepción al INSERT, como debe ocurrir
        en operación normal. Sólo este fixture de ensayo necesita representar
        filas restauradas que ya tenían una marca antigua.
        """
        if connection.vendor != "postgresql":
            return
        tabla = connection.ops.quote_name(instancia._meta.db_table)
        trigger = connection.ops.quote_name(
            f"{instancia._meta.db_table}_first_received_guard_trigger"
        )
        with connection.cursor() as cursor:
            cursor.execute(f"ALTER TABLE {tabla} DISABLE TRIGGER {trigger}")
            try:
                type(instancia).objects.filter(pk=instancia.pk).update(
                    first_received_at=instante
                )
            finally:
                cursor.execute(f"ALTER TABLE {tabla} ENABLE TRIGGER {trigger}")
        instancia.first_received_at = instante

    def pedido(self, *, dias=1, estado=Pedido.Estado.RECIBIDO, macro=None):
        recibido = self.ahora - timedelta(days=dias)
        pedido = Pedido.objects.create(
            sucursal_cliente=self.sucursal,
            macropedido=macro,
            usuario_nombre="prueba",
            estado=estado,
            fecha_confirmacion=recibido,
            first_received_at=recibido,
            total=Decimal("10.00"),
        )
        self.marcar_recepcion_historica(pedido, recibido)
        ItemPedido.objects.create(
            pedido=pedido,
            producto=self.producto,
            cantidad=Decimal("1.000"),
            precio_unitario=Decimal("10.00"),
        )
        return pedido

    def _exportar(self, pedido):
        temporal = tempfile.TemporaryDirectory()
        self.addCleanup(temporal.cleanup)
        archivo = Path(temporal.name) / "lote.zip"
        lote = generar_exportacion(
            desde=pedido.first_received_at - timedelta(minutes=1),
            hasta=pedido.first_received_at + timedelta(minutes=1),
            destino=archivo,
        )
        return lote, archivo

    def test_menos_de_30_dias_no_purga_y_30_dias_si(self):
        reciente = self.pedido(dias=29)
        vencido = self.pedido(dias=30)

        plan = planificar_purga(ahora=self.ahora)

        self.assertNotIn(reciente.pk, plan.pedidos_por_edad)
        self.assertIn(vencido.pk, plan.pedidos_por_edad)

    def test_export_generado_no_confirma_purga(self):
        pedido = self.pedido()
        lote, archivo = self._exportar(pedido)

        plan = planificar_purga(ahora=self.ahora)

        self.assertEqual(lote.estado, ExportacionRetencion.Estado.GENERADA)
        self.assertTrue(archivo.exists())
        self.assertNotIn(pedido.pk, plan.pedidos_por_exportacion)
        self.assertEqual(plan.pedidos_exportacion_no_confirmada, 1)

    def test_export_se_divide_por_id_si_todos_comparten_recepcion(self):
        pedidos = [self.pedido() for _ in range(3)]
        recibido = pedidos[0].first_received_at
        with tempfile.TemporaryDirectory() as temporal:
            desde = recibido - timedelta(minutes=1)
            hasta = recibido + timedelta(minutes=1)
            with self.assertRaisesRegex(ValueError, "excede el limite"):
                generar_exportacion(
                    desde=desde, hasta=hasta,
                    destino=Path(temporal) / "demasiados.zip", limite=2,
                )
            primer = generar_exportacion(
                desde=desde, hasta=hasta,
                destino=Path(temporal) / "primero.zip", limite=2,
                id_hasta=pedidos[2].pk,
            )
            segundo = generar_exportacion(
                desde=desde, hasta=hasta,
                destino=Path(temporal) / "segundo.zip", limite=2,
                id_desde=pedidos[2].pk,
            )
            self.assertEqual(primer.numero_pedidos, 2)
            self.assertEqual(segundo.numero_pedidos, 1)
            self.assertEqual(
                set(primer.miembros.values_list("pedido_id", flat=True))
                | set(segundo.miembros.values_list("pedido_id", flat=True)),
                {pedido.pk for pedido in pedidos},
            )

    def test_hash_erroneo_no_confirma_ni_borra_archivo(self):
        pedido = self.pedido()
        lote, archivo = self._exportar(pedido)

        with self.assertRaisesRegex(ValueError, "SHA-256"):
            confirmar_exportacion(
                lote_id=lote.pk,
                archivo_local=archivo,
                sha256_destino="0" * 64,
                referencia="equipo-prueba",
            )

        lote.refresh_from_db()
        self.assertEqual(lote.estado, ExportacionRetencion.Estado.GENERADA)
        self.assertTrue(archivo.exists())

    def test_zip_manipulado_no_confirma_ni_habilita_purga(self):
        pedido = self.pedido()
        lote, archivo = self._exportar(pedido)
        with archivo.open("ab") as salida:
            salida.write(b"contenido-inyectado")

        with self.assertRaisesRegex(ValueError, "SHA-256"):
            confirmar_exportacion(
                lote_id=lote.pk, archivo_local=archivo,
                sha256_destino=lote.sha256_archivo, referencia="equipo-prueba",
            )

        lote.refresh_from_db()
        self.assertEqual(lote.estado, ExportacionRetencion.Estado.GENERADA)
        self.assertNotIn(pedido.pk, planificar_purga(ahora=self.ahora).pedidos_por_exportacion)

    def test_destino_en_release_o_directorio_publico_se_rechaza(self):
        pedido = self.pedido()
        desde = pedido.first_received_at - timedelta(minutes=1)
        hasta = pedido.first_received_at + timedelta(minutes=1)
        with self.assertRaisesRegex(ValueError, "fuera del release"):
            generar_exportacion(
                desde=desde, hasta=hasta,
                destino=Path(settings.BASE_DIR) / "export-no-publicar.zip",
            )
        with tempfile.TemporaryDirectory() as publico:
            with override_settings(STATIC_ROOT=Path(publico)):
                with self.assertRaisesRegex(ValueError, "directorios públicos"):
                    generar_exportacion(
                        desde=desde, hasta=hasta,
                        destino=Path(publico) / "export-no-publicar.zip",
                    )
        self.assertEqual(ExportacionRetencion.objects.count(), 0)

    def test_export_y_ticket_no_tienen_ruta_web(self):
        pedido = self.pedido()
        lote, archivo = self._exportar(pedido)

        for ruta in (
            f"/api/retencion/exportaciones/{lote.pk}/",
            f"/admin/retencion/exportaciones/{lote.pk}/confirmar/",
            f"/admin/retencion/purgar/?apply=1&lote={lote.pk}",
            f"/static/{archivo.name}",
        ):
            with self.subTest(ruta=ruta):
                self.assertEqual(self.client.get(ruta).status_code, 404)
        self.assertEqual(ExportacionRetencion.objects.get(pk=lote.pk).estado,
                         ExportacionRetencion.Estado.GENERADA)
        self.assertNotIn(ExportacionRetencion, admin.site._registry)
        self.assertTrue(Pedido.objects.filter(pk=pedido.pk).exists())

    def test_cambio_despues_del_export_impide_confirmacion(self):
        pedido = self.pedido()
        lote, archivo = self._exportar(pedido)
        Pedido.objects.filter(pk=pedido.pk).update(total=Decimal("11.00"))

        with self.assertRaisesRegex(ValueError, "cambió"):
            confirmar_exportacion(
                lote_id=lote.pk,
                archivo_local=archivo,
                sha256_destino=lote.sha256_archivo,
                referencia="equipo-prueba",
            )

        self.assertTrue(archivo.exists())
        lote.refresh_from_db()
        self.assertEqual(lote.estado, ExportacionRetencion.Estado.INVALIDADA)
        self.assertNotIn(pedido.pk, planificar_purga(ahora=self.ahora).pedidos_por_exportacion)

    @override_settings(RETENTION_EXPORT_CLEANUP_ENABLED=True)
    def test_zip_invalidado_se_retira_sin_purgar_pedido(self):
        pedido = self.pedido()
        lote, archivo = self._exportar(pedido)
        Pedido.objects.filter(pk=pedido.pk).update(total=Decimal("11.00"))
        with self.assertRaises(ValueError):
            confirmar_exportacion(
                lote_id=lote.pk, archivo_local=archivo,
                sha256_destino=lote.sha256_archivo, referencia="equipo-prueba",
            )
        invalidar_exportacion_obsoleta(lote_id=lote.pk, aplicar=True)
        self.assertFalse(archivo.exists())
        self.assertNotIn(pedido.pk, planificar_purga(ahora=self.ahora).pedidos_por_exportacion)

    def test_export_confirmado_antes_de_30_dias_y_archivo_local_eliminado(self):
        pedido = self.pedido()
        lote, archivo = self._exportar(pedido)

        confirmar_exportacion(
            lote_id=lote.pk,
            archivo_local=archivo,
            sha256_destino=lote.sha256_archivo,
            referencia="equipo-prueba",
        )

        self.assertFalse(archivo.exists())
        self.assertIn(pedido.pk, planificar_purga(ahora=self.ahora).pedidos_por_exportacion)

    def test_confirmacion_doble_no_cambia_ticket_ni_purga(self):
        pedido = self.pedido()
        lote, archivo = self._exportar(pedido)
        contenido = archivo.read_bytes()
        confirmado = confirmar_exportacion(
            lote_id=lote.pk, archivo_local=archivo,
            sha256_destino=lote.sha256_archivo, referencia="primera-verificacion",
        )
        archivo.write_bytes(contenido)

        with self.assertRaisesRegex(ValueError, "GENERADA"):
            confirmar_exportacion(
                lote_id=lote.pk, archivo_local=archivo,
                sha256_destino=lote.sha256_archivo, referencia="segunda-verificacion",
            )

        lote.refresh_from_db()
        self.assertEqual(lote.referencia_confirmacion, "primera-verificacion")
        self.assertEqual(lote.confirmada_en, confirmado.confirmada_en)
        self.assertTrue(Pedido.objects.filter(pk=pedido.pk).exists())
        self.assertEqual(RegistroPurga.objects.count(), 0)

    def test_descarga_operativa_o_estado_enviado_no_confirma_export(self):
        pedido = self.pedido(estado=Pedido.Estado.ENVIADO)

        plan = planificar_purga(ahora=self.ahora)

        self.assertNotIn(pedido.pk, plan.pedidos_por_exportacion)
        self.assertNotIn(pedido.pk, plan.pedidos_por_edad)

    def test_dry_run_repetido_no_escribe(self):
        self.pedido(dias=31)

        primero = planificar_purga(ahora=self.ahora)
        segundo = planificar_purga(ahora=self.ahora)

        self.assertEqual(primero, segundo)
        self.assertEqual(RegistroPurga.objects.count(), 0)

    @override_settings(RETENTION_PURGE_ENABLED=True)
    def test_purga_es_idempotente_y_preserva_maestros(self):
        pedido = self.pedido(dias=31)

        primero = aplicar_purga(ahora=self.ahora)
        segundo = aplicar_purga(ahora=self.ahora)

        self.assertIn(pedido.pk, primero.pedidos_por_edad)
        self.assertEqual(segundo.pedidos_por_edad, ())
        self.assertEqual(Pedido.objects.count(), 0)
        self.assertEqual(ItemPedido.objects.count(), 0)
        self.assertEqual(RegistroPurga.objects.count(), 1)
        self.assertTrue(PedidoPurgado.objects.filter(codigo_publico=pedido.codigo_publico).exists())
        tombstone = PedidoPurgado.objects.get(codigo_publico=pedido.codigo_publico)
        self.assertEqual(tombstone.motivo, "antiguedad")
        self.assertEqual(tombstone.sucursal_cliente_id, self.sucursal.pk)
        self.assertEqual(tombstone.fecha_confirmacion, pedido.fecha_confirmacion)
        self.assertEqual(SucursalCliente.objects.count(), 1)
        self.assertEqual(Producto.objects.count(), 1)
        self.assertEqual(Precio.objects.count(), 1)
        self.assertTrue(Configuracion.objects.filter(pk=self.configuracion.pk).exists())

    @override_settings(RETENTION_PURGE_ENABLED=True)
    def test_lotes_mezclados_y_macro_con_hijo_restante(self):
        macro = MacroPedido.objects.create(
            sucursal_cliente=self.sucursal,
            fecha_pedido=timezone.localdate(),
            ultima_confirmacion=self.ahora,
            total=Decimal("20.00"),
        )
        viejo = self.pedido(dias=31, macro=macro)
        nuevo = self.pedido(dias=1, macro=macro)

        aplicar_purga(ahora=self.ahora)

        self.assertFalse(Pedido.objects.filter(pk=viejo.pk).exists())
        self.assertTrue(Pedido.objects.filter(pk=nuevo.pk).exists())
        macro.refresh_from_db()
        self.assertEqual(macro.total, Decimal("10.00"))

    @override_settings(RETENTION_PURGE_ENABLED=True)
    def test_pedido_abierto_vencido_bloquea_purga(self):
        abierto = self.pedido(dias=31, estado=Pedido.Estado.CONFIRMADO)

        plan = planificar_purga(ahora=self.ahora)

        self.assertIn(abierto.pk, plan.pedidos_bloqueados_abiertos)
        with self.assertRaisesRegex(RuntimeError, "decisión del dueño"):
            aplicar_purga(ahora=self.ahora)
        self.assertTrue(Pedido.objects.filter(pk=abierto.pk).exists())

    def test_purga_real_inhabilitada_por_defecto(self):
        self.pedido(dias=31)

        with self.assertRaisesRegex(RuntimeError, "deshabilitada"):
            aplicar_purga(ahora=self.ahora)

    @override_settings(RETENTION_PURGE_ENABLED=True, RETENTION_RESTORE_ISOLATED=True)
    def test_restore_reintroducido_se_detecta_y_elimina(self):
        pedido = self.pedido(dias=31)
        codigo = pedido.codigo_publico
        aplicar_purga(ahora=self.ahora)
        Pedido.objects.create(
            sucursal_cliente=self.sucursal,
            codigo_publico=codigo,
            usuario_nombre="restaurado",
            estado=Pedido.Estado.RECIBIDO,
            first_received_at=self.ahora - timedelta(days=31),
        )

        self.assertEqual(conciliar_restauracion()["pedidos_reintroducidos"], 1)
        self.assertEqual(conciliar_restauracion(aplicar=True)["pedidos_reintroducidos"], 1)
        self.assertEqual(conciliar_restauracion()["pedidos_reintroducidos"], 0)

    @override_settings(RETENTION_PURGE_ENABLED=True)
    def test_procedencia_lote_y_eventos_asociados_se_eliminan(self):
        pedido = self.pedido()
        lote, archivo = self._exportar(pedido)
        confirmar_exportacion(
            lote_id=lote.pk, archivo_local=archivo,
            sha256_destino=lote.sha256_archivo, referencia="equipo-prueba",
        )
        evento = EventoCliente.objects.create(
            evento="confirmar_exito_servidor",
            detalle={"pedido_id": str(pedido.pk), "mensaje": "dato transitorio"},
            first_received_at=self.ahora,
        )
        aplicar_purga(ahora=self.ahora)
        tombstone = PedidoPurgado.objects.get(codigo_publico=pedido.codigo_publico)
        self.assertEqual(tombstone.motivo, "exportacion")
        self.assertEqual(tombstone.exportacion_id, lote.pk)
        self.assertFalse(EventoCliente.objects.filter(pk=evento.pk).exists())
        self.assertEqual(
            ocultar_referencia_pedido_purgado(
                {"pedido_id": str(pedido.pk), "mensaje": "dato transitorio"}
            ),
            {"retencion": "pedido_purgado"},
        )

    @override_settings(RETENTION_PURGE_ENABLED=True)
    def test_evento_solo_se_purga_por_primera_recepcion_vps(self):
        antiguo = EventoCliente.objects.create(
            evento="prueba", first_received_at=self.ahora - timedelta(days=31)
        )
        self.marcar_recepcion_historica(antiguo, self.ahora - timedelta(days=31))
        nuevo = EventoCliente.objects.create(evento="prueba", first_received_at=self.ahora)
        sin_marca = EventoCliente.objects.create(evento="prueba")
        plan = aplicar_purga(ahora=self.ahora)
        self.assertEqual(plan.eventos_vencidos, 1)
        self.assertFalse(EventoCliente.objects.filter(pk=antiguo.pk).exists())
        self.assertTrue(EventoCliente.objects.filter(pk=nuevo.pk).exists())
        self.assertTrue(EventoCliente.objects.filter(pk=sin_marca.pk).exists())

    def test_restaura_sin_flag_es_solo_dry_run(self):
        self.assertEqual(conciliar_restauracion(), {"pedidos_reintroducidos": 0})
