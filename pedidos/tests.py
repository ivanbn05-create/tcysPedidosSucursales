import json
from datetime import datetime, time, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from .apps import COMANDOS_SIN_SCHEDULER
from .models import (
    CONFIGURACION_CACHE_KEY,
    Configuracion,
    ItemPedido,
    LogRecordatorio,
    MacroPedido,
    Pedido,
    Precio,
    Producto,
    SucursalCliente,
)
from .seed import CLIENTES_DEMO, password_for_cliente, seed_demo_data


def abrir_horario_completo():
    """Deja el horario de pedidos abierto todo el día, para que las pruebas que
    no son sobre restricción horaria no dependan de la hora real de ejecución."""

    config = Configuracion.get_solo()
    config.hora_inicio_pedidos = time(0, 0)
    config.hora_fin_pedidos = time(23, 59)
    config.save()
    cache.delete(CONFIGURACION_CACHE_KEY)
    return config


class PedidoFlowTests(TestCase):
    def setUp(self):
        seed_demo_data()
        abrir_horario_completo()

    def test_configuracion_estaticos_mantiene_whitenoise(self):
        self.assertIn("whitenoise.middleware.WhiteNoiseMiddleware", settings.MIDDLEWARE)
        self.assertIn("compactar_precios", COMANDOS_SIN_SCHEDULER)

    def test_css_movil_no_bloquea_scroll_global(self):
        responsive_css = Path(settings.BASE_DIR, "static", "css", "responsive.css").read_text(
            encoding="utf-8"
        )
        styles_css = Path(settings.BASE_DIR, "static", "css", "styles.css").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("html,\n    body.order-page", responsive_css.replace("\r\n", "\n"))
        self.assertIn("body.order-page {\n        height: 100dvh;", responsive_css.replace("\r\n", "\n"))
        for color in ("#20ad69", "#86c83e", "#f2d33b", "#f19a32", "#e64b43"):
            self.assertIn(color, styles_css)

    def crear_pedido_confirmado(
        self,
        sucursal_nombre,
        items,
        fecha_confirmacion=None,
        estado=Pedido.Estado.CONFIRMADO,
    ):
        sucursal = SucursalCliente.objects.get(nombre=sucursal_nombre)
        fecha_confirmacion = fecha_confirmacion or timezone.now()
        fecha_pedido = timezone.localtime(fecha_confirmacion).date()
        macropedido, _ = MacroPedido.objects.get_or_create(
            sucursal_cliente=sucursal,
            fecha_pedido=fecha_pedido,
            defaults={
                "ultima_confirmacion": fecha_confirmacion,
                "estado": estado,
            },
        )
        pedido = Pedido.objects.create(
            sucursal_cliente=sucursal,
            macropedido=macropedido,
            usuario_nombre=sucursal.nombre,
            estado=estado,
            fecha_confirmacion=fecha_confirmacion,
        )
        for producto_nombre, cantidad in items:
            producto = Producto.objects.get(nombre=producto_nombre)
            ItemPedido.objects.create(
                pedido=pedido,
                producto=producto,
                cantidad=Decimal(str(cantidad)),
                precio_unitario=Decimal("1.00"),
            )
        pedido.recalcular_total()
        estados = set(macropedido.pedidos.filter(eliminado=False).values_list("estado", flat=True))
        if Pedido.Estado.CONFIRMADO in estados:
            macropedido.estado = MacroPedido.Estado.CONFIRMADO
        elif Pedido.Estado.ENVIADO in estados:
            macropedido.estado = MacroPedido.Estado.ENVIADO
        else:
            macropedido.estado = MacroPedido.Estado.RECIBIDO
        macropedido.ultima_confirmacion = max(
            fecha_confirmacion,
            macropedido.ultima_confirmacion,
        )
        macropedido.save(update_fields=["estado", "ultima_confirmacion", "fecha_actualizacion"])
        macropedido.recalcular_resumen()
        return pedido

    def test_login_crear_confirmar_e_imprimir(self):
        self.assertTrue(self.client.login(username="aguilas", password="Aguilas8445"))
        producto = Producto.objects.get(nombre="LITRO DE BARBACOA")

        response = self.client.post(
            "/api/pedidos/crear-item/",
            data=json.dumps({"producto_id": producto.id, "cantidad": "2.5"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["total_pedido"], "482.50")

        response = self.client.post("/api/pedidos/confirmar/", content_type="application/json")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertIn("pedido_folio", data)
        self.assertEqual(data["pedidos_del_dia"], 1)
        self.assertEqual(data["max_pedidos_dia"], 5)
        self.assertNotIn("#", data["mensaje"])

        pedido = Pedido.objects.get(id=data["pedido_id"])
        macropedido = pedido.macropedido
        self.assertEqual(pedido.estado, Pedido.Estado.CONFIRMADO)
        self.assertEqual(pedido.total, Decimal("482.50"))
        self.assertEqual(macropedido.total, Decimal("482.50"))
        self.assertEqual(macropedido.cantidad_pedidos, 1)

        self.client.logout()
        self.assertTrue(self.client.login(username="juancarlos", password="TocayosMO2026"))
        print_response = self.client.get(f"/admin/pedidos/{pedido.id}/imprimir/")
        self.assertEqual(print_response.status_code, 200)
        self.assertContains(print_response, "window.print()")
        self.assertContains(print_response, "size: 72mm 73mm;")
        self.assertContains(print_response, 'class="ticket-item-row"', count=1)
        self.assertContains(print_response, "AGUILAS")
        self.assertContains(print_response, "BARBACOA")
        self.assertContains(print_response, "2.5 KG")
        self.assertNotContains(print_response, "$482.50")

        dashboard = self.client.get("/admin/")
        self.assertContains(dashboard, macropedido.folio_dia)
        self.assertContains(dashboard, "&Uacute;ltimo pedido")
        self.assertContains(dashboard, 'aria-label="1 de 5 pedidos realizados"')
        self.assertContains(dashboard, "data-inline-print")
        self.assertContains(dashboard, f'data-print-template-id="print-macro-{macropedido.id}"')
        self.assertContains(dashboard, f'data-print-template-id="print-pedido-{pedido.id}"')
        self.assertContains(dashboard, 'id="inlinePrintSurface"')
        self.assertContains(dashboard, f'id="print-pedido-{pedido.id}"')
        self.assertNotContains(dashboard, 'target="_blank"')
        self.assertNotContains(dashboard, f"#{pedido.id}")

        embedded_print = self.client.get(f"/admin/pedidos/{pedido.id}/imprimir/?embedded=1")
        self.assertEqual(embedded_print.status_code, 200)
        self.assertNotContains(embedded_print, 'window.addEventListener("load"')

    def test_ticket_imprime_solo_una_fila_por_producto_pedido(self):
        self.assertTrue(self.client.login(username="aguilas", password="Aguilas8445"))
        productos = list(
            Producto.objects.filter(precios__sucursal_cliente__nombre="Aguilas")
            .distinct()
            .order_by("orden", "nombre")[:5]
        )
        self.assertEqual(len(productos), 5)
        for producto in productos:
            response = self.client.post(
                "/api/pedidos/crear-item/",
                data=json.dumps({"producto_id": producto.id, "cantidad": "1"}),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 200)

        response = self.client.post("/api/pedidos/confirmar/", content_type="application/json")
        pedido_id = response.json()["pedido_id"]

        self.client.logout()
        self.assertTrue(self.client.login(username="juancarlos", password="TocayosMO2026"))
        print_response = self.client.get(f"/admin/pedidos/{pedido_id}/imprimir/")
        self.assertContains(print_response, 'class="ticket-item-row"', count=5)

    def test_cliente_mayorista_usa_precio_dos_pesos(self):
        self.assertTrue(self.client.login(username="brot_nueva_galicia", password="Brot Nueva Galicia0846"))
        producto = Producto.objects.get(nombre="TORTILLA ESPECIAL")
        response = self.client.post(
            "/api/pedidos/crear-item/",
            data=json.dumps({"producto_id": producto.id, "cantidad": "3"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total_pedido"], "79.50")

    def test_chile_guero_se_cobra_por_kilo_promedio_de_treinta_piezas(self):
        self.assertTrue(self.client.login(username="aguilas", password="Aguilas8445"))
        producto = Producto.objects.get(nombre="CHILE GüERO")
        response = self.client.post(
            "/api/pedidos/crear-item/",
            data=json.dumps({"producto_id": producto.id, "cantidad": "30"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total_pedido"], "64.00")
        self.assertEqual(data["pedido"]["items"][0]["unidad"], "PZA")

    def test_ticket_mayoreo_marca_productos_con_sufijo_m(self):
        self.assertTrue(self.client.login(username="brot_nueva_galicia", password="Brot Nueva Galicia0846"))
        producto = Producto.objects.get(nombre="LITRO DE BARBACOA")
        response = self.client.post(
            "/api/pedidos/crear-item/",
            data=json.dumps({"producto_id": producto.id, "cantidad": "1"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        response = self.client.post("/api/pedidos/confirmar/", content_type="application/json")
        pedido_id = response.json()["pedido_id"]

        self.client.logout()
        self.assertTrue(self.client.login(username="juancarlos", password="TocayosMO2026"))
        response = self.client.get(f"/admin/pedidos/{pedido_id}/imprimir/")
        self.assertContains(response, "BARBACOA .M")

    def test_ticket_imprime_la_cantidad_capturada_sin_bonificaciones(self):
        """La promoción de martes de Águilas se eliminó por completo: el ticket
        debe mostrar exactamente lo capturado, cualquier día de la semana."""

        self.assertTrue(self.client.login(username="aguilas", password="Aguilas8445"))
        producto = Producto.objects.get(nombre="LITRO DE BARBACOA")
        response = self.client.post(
            "/api/pedidos/crear-item/",
            data=json.dumps({"producto_id": producto.id, "cantidad": "20"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total_pedido"], "3860.00")
        response = self.client.post("/api/pedidos/confirmar/", content_type="application/json")
        pedido_id = response.json()["pedido_id"]

        pedido = Pedido.objects.get(id=pedido_id)
        martes = timezone.make_aware(datetime(2026, 7, 14, 10, 0))
        pedido.fecha_confirmacion = martes
        pedido.save(update_fields=["fecha_confirmacion"])

        self.client.logout()
        self.assertTrue(self.client.login(username="juancarlos", password="TocayosMO2026"))
        response = self.client.get(f"/admin/pedidos/{pedido_id}/imprimir/")
        self.assertContains(response, "20 KG")
        self.assertEqual(pedido.total, Decimal("3860.00"))

    def test_promocion_martes_no_existe_en_ninguna_capa(self):
        self.assertFalse(hasattr(Producto, "promo_aguilas_martes"))
        self.assertNotIn(
            "promo_aguilas_martes",
            [campo.name for campo in Producto._meta.get_fields()],
        )
        self.assertFalse(hasattr(ItemPedido, "cantidad_con_promocion"))
        self.assertFalse(hasattr(ItemPedido, "cantidad_bonificacion"))
        self.assertFalse(hasattr(ItemPedido, "aplica_promo_aguilas_martes"))

        self.assertTrue(self.client.login(username="juancarlos", password="TocayosMO2026"))
        response = self.client.get("/admin/configuracion/")
        self.assertNotContains(response, "promo_aguilas_martes")
        self.assertNotContains(response, "Promo martes")

    def test_agregar_producto_existente_reemplaza_cantidad(self):
        self.assertTrue(self.client.login(username="aguilas", password="Aguilas8445"))
        producto = Producto.objects.get(nombre="LITRO DE BARBACOA")

        first = self.client.post(
            "/api/pedidos/crear-item/",
            data=json.dumps({"producto_id": producto.id, "cantidad": "5"}),
            content_type="application/json",
        )
        second = self.client.post(
            "/api/pedidos/crear-item/",
            data=json.dumps({"producto_id": producto.id, "cantidad": "3"}),
            content_type="application/json",
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        data = second.json()
        self.assertEqual(data["total_pedido"], "579.00")
        self.assertEqual(len(data["pedido"]["items"]), 1)
        self.assertEqual(data["pedido"]["items"][0]["cantidad"], "3.000")

    def test_primer_item_se_muestra_si_habia_pedido_pendiente_vacio(self):
        self.assertTrue(self.client.login(username="aguilas", password="Aguilas8445"))
        sucursal = SucursalCliente.objects.get(nombre="Aguilas")
        Pedido.objects.create(sucursal_cliente=sucursal, usuario_nombre=sucursal.nombre)
        producto = Producto.objects.get(nombre="SALSA DE AGUACATE")

        response = self.client.post(
            "/api/pedidos/crear-item/",
            data=json.dumps({"producto_id": producto.id, "cantidad": "1"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total_pedido"], "60.00")
        self.assertEqual(len(data["pedido"]["items"]), 1)
        self.assertEqual(data["pedido"]["items"][0]["producto"], "SALSA DE AGUACATE")
        self.assertEqual(data["pedido"]["items"][0]["cantidad"], "1.000")

    def test_seed_actualiza_precios_etiqueta_aguacate_y_orden_aguas(self):
        aguilas = SucursalCliente.objects.get(nombre="Aguilas")
        mayorista = SucursalCliente.objects.get(nombre="Brot Nueva Galicia")
        barbacoa = Producto.objects.get(nombre="LITRO DE BARBACOA")
        salsa_aguacate = Producto.objects.get(nombre="SALSA DE AGUACATE")
        aguas = [
            "AGUA HORCHATA BLANCA 1/2",
            "AGUA HORCHATA BLANCA LT",
            "AGUA HORCHATA ROSA 1/2",
            "AGUA HORCHATA ROSA LT",
            "AGUA JAMAICA 1/2",
            "AGUA JAMAICA LT",
        ]

        self.assertEqual(salsa_aguacate.nombre_ticket, "S. AGUACATE")
        self.assertEqual(
            list(Producto.objects.filter(nombre__in=aguas).order_by("orden").values_list("nombre", flat=True)),
            aguas,
        )
        self.assertEqual(
            Precio.objects.get(producto=barbacoa, sucursal_cliente=aguilas).precio_unitario,
            Decimal("193.00"),
        )
        self.assertEqual(
            Precio.objects.get(producto=barbacoa, sucursal_cliente=mayorista).precio_unitario,
            Decimal("203.00"),
        )
        for sucursal in (aguilas, mayorista):
            self.assertEqual(
                Precio.objects.get(producto__nombre="AGUA HORCHATA BLANCA 1/2", sucursal_cliente=sucursal).precio_unitario,
                Decimal("19.00"),
            )
            self.assertEqual(
                Precio.objects.get(producto__nombre="AGUA JAMAICA LT", sucursal_cliente=sucursal).precio_unitario,
                Decimal("32.00"),
            )
            self.assertEqual(
                Precio.objects.get(producto=salsa_aguacate, sucursal_cliente=sucursal).nombre_ticket,
                "S. AGUACATE",
            )

        self.assertTrue(self.client.login(username="aguilas", password="Aguilas8445"))
        for product_name in (
            "AGUA HORCHATA ROSA 1/2",
            "AGUA JAMAICA 1/2",
            "AGUA HORCHATA BLANCA 1/2",
        ):
            producto = Producto.objects.get(nombre=product_name)
            response = self.client.post(
                "/api/pedidos/crear-item/",
                data=json.dumps({"producto_id": producto.id, "cantidad": "1"}),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 200)

        response = self.client.post("/api/pedidos/confirmar/", content_type="application/json")
        pedido_id = response.json()["pedido_id"]
        self.client.logout()
        self.assertTrue(self.client.login(username="juancarlos", password="TocayosMO2026"))

        print_response = self.client.get(f"/admin/pedidos/{pedido_id}/imprimir/?embedded=1")
        html = print_response.content.decode()
        self.assertLess(html.index("HB 1/2"), html.index("HR 1/2"))
        self.assertLess(html.index("HR 1/2"), html.index("JAM 1/2"))

    def test_seed_no_duplica_precios_si_no_cambia_el_valor_vigente(self):
        total_inicial = Precio.objects.count()
        manana = timezone.localdate() + timedelta(days=1)

        with patch("pedidos.seed.timezone.localdate", return_value=manana):
            seed_demo_data()

        self.assertEqual(Precio.objects.count(), total_inicial)

    def test_seed_crea_historial_de_precio_solo_si_cambia_el_valor(self):
        aguilas = SucursalCliente.objects.get(nombre="Aguilas")
        barbacoa = Producto.objects.get(nombre="LITRO DE BARBACOA")
        precio_actual = Precio.objects.get(producto=barbacoa, sucursal_cliente=aguilas)
        precio_actual.precio_unitario = Decimal("178.00")
        precio_actual.save(update_fields=["precio_unitario"])
        total_inicial = Precio.objects.count()
        manana = timezone.localdate() + timedelta(days=1)

        with patch("pedidos.seed.timezone.localdate", return_value=manana):
            seed_demo_data()

        self.assertEqual(Precio.objects.count(), total_inicial + 1)
        self.assertTrue(
            Precio.objects.filter(
                producto=barbacoa,
                sucursal_cliente=aguilas,
                fecha_vigencia=manana,
                precio_unitario=Decimal("193.00"),
                nombre_ticket="BARBACOA",
            ).exists()
        )

    def test_compactar_precios_dry_run_y_elimina_solo_redundantes(self):
        aguilas = SucursalCliente.objects.get(nombre="Aguilas")
        barbacoa = Producto.objects.get(nombre="LITRO DE BARBACOA")
        hoy = timezone.localdate()
        precio_conservado = Precio.objects.create(
            producto=barbacoa,
            sucursal_cliente=aguilas,
            fecha_vigencia=hoy - timedelta(days=2),
            precio_unitario=Decimal("178.00"),
            nombre_ticket="BARBACOA",
        )
        precio_redundante = Precio.objects.create(
            producto=barbacoa,
            sucursal_cliente=aguilas,
            fecha_vigencia=hoy - timedelta(days=1),
            precio_unitario=Decimal("178.00"),
            nombre_ticket="BARBACOA",
        )
        precio_vigente = Precio.objects.get(producto=barbacoa, sucursal_cliente=aguilas, fecha_vigencia=hoy)

        salida = StringIO()
        call_command("compactar_precios", dry_run=True, stdout=salida)
        self.assertIn("DRY-RUN", salida.getvalue())
        self.assertTrue(Precio.objects.filter(id=precio_redundante.id).exists())

        call_command("compactar_precios", stdout=StringIO())

        self.assertTrue(Precio.objects.filter(id=precio_conservado.id).exists())
        self.assertFalse(Precio.objects.filter(id=precio_redundante.id).exists())
        self.assertTrue(Precio.objects.filter(id=precio_vigente.id).exists())

    def test_pantalla_pedidos_no_muestra_precios_unitarios(self):
        self.assertTrue(self.client.login(username="aguilas", password="Aguilas8445"))
        response = self.client.get("/pedidos/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'href="/pedidos/historial/"')
        self.assertContains(response, ">Historial</a>")
        self.assertContains(response, "Pedidos de hoy")
        self.assertContains(response, 'aria-label="0 de 5 pedidos realizados hoy"')
        self.assertContains(response, 'data-daily-segment="5"')
        self.assertNotContains(response, '<span class="brand-title">Pedidos</span>')
        self.assertNotContains(response, "$193.00")
        self.assertNotContains(response, "precio_unitario")
        self.assertNotContains(response, "scheduleStatus")
        self.assertNotContains(response, "Total tentativo")

    def test_paginas_de_sesion_no_se_pueden_cachear(self):
        """Sin `Cache-Control: no-store` el WebView in-app puede volver a mostrar
        una copia guardada de /login/ o /pedidos/ (con datos de otra sesión), y la
        petición ni siquiera llega al servidor."""

        self.assertTrue(self.client.login(username="aguilas", password="Aguilas8445"))
        rutas = ["/pedidos/", "/pedidos/historial/", "/api/horarios/"]
        for ruta in rutas:
            with self.subTest(ruta=ruta):
                response = self.client.get(ruta)
                self.assertIn("no-store", response.headers.get("Cache-Control", ""))

        self.client.logout()
        for ruta in ["/", "/login/"]:
            with self.subTest(ruta=ruta):
                response = self.client.get(ruta)
                self.assertIn("no-store", response.headers.get("Cache-Control", ""))

    def test_cookies_de_sesion_usan_samesite_lax(self):
        """Con "Strict" la cookie no viaja al abrir el sistema desde un link de
        WhatsApp o de la app de Google, y la sucursal caería siempre en el login."""

        self.assertEqual(settings.SESSION_COOKIE_SAMESITE, "Lax")
        self.assertEqual(settings.CSRF_COOKIE_SAMESITE, "Lax")

    def test_login_bloquea_el_doble_envio_del_formulario(self):
        response = self.client.get("/login/")
        self.assertContains(response, "data-login-submit")
        self.assertContains(response, "Entrando...")
        # El bloqueo se libera solo: ni bfcache ni una red caída deben dejar al
        # usuario sin poder reintentar.
        self.assertContains(response, "pageshow")

    def test_pedidos_confirma_con_modal_propio_y_no_con_window_confirm(self):
        """En navegadores in-app de iOS `window.confirm()` puede devolver false sin
        mostrar diálogo, dejando "Confirmar pedido" y "Limpiar pedido" mudos."""

        self.assertTrue(self.client.login(username="aguilas", password="Aguilas8445"))
        response = self.client.get("/pedidos/")

        self.assertContains(response, 'id="confirmModal"')
        self.assertContains(response, 'id="confirmAccept"')
        self.assertContains(response, 'id="confirmCancel"')

        pedidos_js = Path(settings.BASE_DIR, "static", "js", "pedidos.js").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("window.confirm(", pedidos_js)
        self.assertIn("askConfirm(", pedidos_js)
        self.assertIn("AbortController", pedidos_js)

    def test_log_cliente_registra_evento_y_no_toca_el_pedido(self):
        self.assertTrue(self.client.login(username="aguilas", password="Aguilas8445"))
        pedidos_antes = Pedido.objects.count()

        with self.assertLogs("pedidos.views", level="INFO") as registro:
            response = self.client.post(
                "/api/pedidos/log-cliente/",
                data=json.dumps({"evento": "confirmar_click", "items": 3}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertIn("confirmar_click", "\n".join(registro.output))
        self.assertEqual(Pedido.objects.count(), pedidos_antes)

    def test_log_cliente_requiere_sesion(self):
        response = self.client.post(
            "/api/pedidos/log-cliente/",
            data=json.dumps({"evento": "confirmar_click"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 302)

    def test_usuario_ve_historial_propio_e_imprime_recibo_adaptable(self):
        fecha_reciente = timezone.make_aware(datetime(2026, 7, 17, 12, 0))
        fecha_anterior = timezone.make_aware(datetime(2026, 7, 16, 12, 0))
        fecha_otro = timezone.make_aware(datetime(2026, 7, 15, 12, 0))
        reciente = self.crear_pedido_confirmado(
            "Aguilas",
            [
                ("LITRO DE BARBACOA", "2"),
                ("TORTILLA ESPECIAL", "3"),
            ],
            fecha_reciente,
        )
        reciente_dos = self.crear_pedido_confirmado(
            "Aguilas",
            [("LITRO DE BARBACOA", "1")],
            fecha_reciente + timedelta(hours=1),
        )
        anterior = self.crear_pedido_confirmado(
            "Aguilas",
            [("AGUA JAMAICA LT", "4")],
            fecha_anterior,
        )
        otro = self.crear_pedido_confirmado(
            "Fortin",
            [("LITRO DE BARBACOA", "5")],
            fecha_otro,
        )

        self.assertTrue(self.client.login(username="aguilas", password="Aguilas8445"))
        response = self.client.get("/pedidos/historial/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Historial")
        self.assertContains(response, "17/07/2026")
        self.assertContains(response, "16/07/2026")
        self.assertContains(response, "13:00")
        self.assertContains(response, "2/5 pedidos")
        self.assertContains(response, 'aria-label="2 de 5 pedidos realizados"')
        self.assertContains(response, str(reciente.codigo_publico))
        self.assertContains(response, str(reciente_dos.codigo_publico))
        self.assertNotContains(response, otro.macropedido.folio_dia)
        self.assertNotContains(response, f"Pedido #{reciente.id}")
        html = response.content.decode()
        self.assertLess(
            html.index("17/07/2026"),
            html.index("16/07/2026"),
        )
        self.assertContains(response, 'data-print-size="auto"')
        self.assertContains(
            response,
            f'data-print-template-id="print-history-pedido-{reciente.codigo_publico}"',
        )
        self.assertContains(response, "Total provisional")
        self.assertNotContains(response, "precio_unitario")

        print_response = self.client.get(f"/pedidos/historial/{reciente.codigo_publico}/imprimir/")
        self.assertEqual(print_response.status_code, 200)
        self.assertContains(print_response, "size: auto;")
        self.assertContains(print_response, "window.print()")
        self.assertContains(print_response, "Aguilas")
        self.assertContains(print_response, reciente.folio_fecha)
        self.assertContains(print_response, str(reciente.codigo_publico).split("-")[0].upper())
        self.assertContains(print_response, "LITRO DE BARBACOA")
        self.assertContains(print_response, "2 KG")
        self.assertContains(print_response, "Total provisional")
        self.assertNotContains(print_response, "$1.00")
        self.assertNotContains(print_response, "precio_unitario")
        self.assertNotContains(print_response, f"#{reciente.id}")

        macro = reciente.macropedido
        macro.refresh_from_db()
        macro_print = self.client.get(
            f"/pedidos/historial/dia/{macro.codigo_publico}/imprimir/"
        )
        self.assertEqual(macro_print.status_code, 200)
        self.assertContains(macro_print, macro.folio_fecha)
        self.assertContains(macro_print, "3 KG")
        self.assertContains(macro_print, "Total provisional: $6.00")

        embedded = self.client.get(f"/pedidos/historial/{reciente.codigo_publico}/imprimir/?embedded=1")
        self.assertEqual(embedded.status_code, 200)
        self.assertNotContains(embedded, 'window.addEventListener("load"')

        forbidden = self.client.get(f"/pedidos/historial/{otro.codigo_publico}/imprimir/")
        self.assertEqual(forbidden.status_code, 404)

    def test_login_muestra_horario_de_pedidos(self):
        response = self.client.get("/login/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pedidos abiertos")
        self.assertContains(response, "data-password-toggle")
        self.assertContains(response, 'aria-label="Mostrar contraseña"')
        self.assertContains(response, "Sistema privado de uso exclusivo")
        self.assertContains(response, 'href="/privacidad/"')

    def test_aviso_privacidad_publico_y_link_interno(self):
        response = self.client.get("/privacidad/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Aviso de privacidad")
        self.assertContains(response, "Cookies técnicas")
        self.assertContains(response, "tocayos.tacos@gmail.com")

        self.assertTrue(self.client.login(username="aguilas", password="Aguilas8445"))
        response = self.client.get("/pedidos/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="privacy-toplink"')
        self.assertContains(response, 'href="/privacidad/"')

    def test_usuario_no_admin_no_puede_ver_dashboard(self):
        self.assertTrue(self.client.login(username="fortin", password="Fortin9481"))
        response = self.client.get("/admin/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/pedidos/")

        response = self.client.get("/admin/configuracion/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/pedidos/")

    def test_usuario_impresion_solo_ve_dashboard_e_imprime(self):
        self.assertTrue(self.client.login(username="aguilas", password="Aguilas8445"))
        producto = Producto.objects.get(nombre="LITRO DE BARBACOA")
        self.client.post(
            "/api/pedidos/crear-item/",
            data=json.dumps({"producto_id": producto.id, "cantidad": "1"}),
            content_type="application/json",
        )
        response = self.client.post("/api/pedidos/confirmar/", content_type="application/json")
        pedido_id = response.json()["pedido_id"]
        macropedido_id = response.json()["macropedido_id"]
        self.client.logout()

        self.assertTrue(self.client.login(username="juanmanuel", password="imprimir"))
        dashboard = self.client.get("/admin/")
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, "data-macro-toggle")
        self.assertContains(dashboard, "Imprimir acumulado")
        self.assertContains(dashboard, "Imprimir")
        self.assertContains(dashboard, "Aguas")
        self.assertContains(dashboard, "data-inline-print")
        self.assertContains(dashboard, 'data-print-template-id="print-aguas"')
        self.assertContains(dashboard, f'id="print-macro-{macropedido_id}"')
        self.assertContains(dashboard, f'id="print-pedido-{pedido_id}"')
        html = dashboard.content.decode()
        self.assertNotIn('target="_blank"', html)
        self.assertNotIn("admin/configuracion", html)
        self.assertNotIn("admin/datos", html)
        self.assertNotIn(">Excel</a>", html)
        self.assertNotIn("marcar-enviado", html)
        self.assertNotIn("revertir-enviado", html)
        self.assertNotIn("eliminar/", html)

        print_response = self.client.get(f"/admin/pedidos/{pedido_id}/imprimir/")
        self.assertEqual(print_response.status_code, 200)
        self.assertContains(print_response, "window.print()")
        aguas_response = self.client.get("/admin/aguas/imprimir/")
        self.assertEqual(aguas_response.status_code, 200)
        self.assertContains(aguas_response, "size: 72mm 72mm;")
        aguas_embedded = self.client.get("/admin/aguas/imprimir/?embedded=1")
        self.assertEqual(aguas_embedded.status_code, 200)
        self.assertNotContains(aguas_embedded, 'window.addEventListener("load"')

        response = self.client.get("/admin/configuracion/")
        self.assertEqual(response.status_code, 302)
        response = self.client.get("/admin/datos/")
        self.assertEqual(response.status_code, 302)
        response = self.client.get(f"/admin/pedidos/{pedido_id}/excel/")
        self.assertEqual(response.status_code, 404)
        response = self.client.get(f"/admin/pedidos/{pedido_id}/descargar/")
        self.assertEqual(response.status_code, 404)
        response = self.client.post(f"/admin/pedidos/{pedido_id}/eliminar/")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Pedido.objects.get(id=pedido_id).eliminado)

    def test_admin_puede_marcar_enviado_y_deshacer_el_envio(self):
        pedido = self.crear_pedido_confirmado(
            "Aguilas",
            [("LITRO DE BARBACOA", "2")],
        )
        macropedido = pedido.macropedido
        self.assertTrue(self.client.login(username="juancarlos", password="TocayosMO2026"))

        dashboard = self.client.get("/admin/")
        self.assertNotContains(dashboard, ">Excel</a>")
        self.assertNotContains(dashboard, f"/admin/pedidos/{pedido.id}/excel/")
        self.assertContains(dashboard, f"/admin/macropedidos/{macropedido.id}/marcar-enviado/")
        self.assertNotContains(
            dashboard,
            f"/admin/macropedidos/{macropedido.id}/revertir-enviado/",
        )

        response = self.client.post(f"/admin/macropedidos/{macropedido.id}/marcar-enviado/")
        self.assertRedirects(response, "/admin/?estado=enviado")
        pedido.refresh_from_db()
        macropedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.Estado.ENVIADO)
        self.assertEqual(macropedido.estado, MacroPedido.Estado.ENVIADO)

        dashboard = self.client.get("/admin/?estado=enviado")
        self.assertContains(dashboard, "Deshacer env&iacute;o", html=True)
        self.assertContains(
            dashboard,
            f"/admin/macropedidos/{macropedido.id}/revertir-enviado/",
        )
        self.assertNotContains(
            dashboard,
            f"/admin/macropedidos/{macropedido.id}/marcar-enviado/",
        )

        response = self.client.post(f"/admin/macropedidos/{macropedido.id}/revertir-enviado/")
        self.assertRedirects(response, "/admin/?estado=confirmado")
        pedido.refresh_from_db()
        macropedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.Estado.CONFIRMADO)
        self.assertEqual(macropedido.estado, MacroPedido.Estado.CONFIRMADO)

        self.assertEqual(self.client.get(f"/admin/pedidos/{pedido.id}/excel/").status_code, 404)
        self.assertEqual(self.client.get(f"/admin/pedidos/{pedido.id}/descargar/").status_code, 404)

    @patch("pedidos.views.ADMIN_DASHBOARD_PAGE_SIZE", 2)
    def test_admin_dashboard_pagina_y_evitar_consultas_por_item(self):
        fecha_base = timezone.make_aware(datetime(2026, 7, 20, 10, 0))
        for index in range(3):
            self.crear_pedido_confirmado(
                "Aguilas",
                [("LITRO DE BARBACOA", "1")],
                fecha_base - timedelta(days=index),
            )

        self.assertTrue(self.client.login(username="juancarlos", password="TocayosMO2026"))
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get("/admin/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pagina 1 de 2")
        html = response.content.decode()
        self.assertEqual(html.count('<template\n    id="print-macro-'), 2)
        self.assertEqual(html.count('<template\n    id="print-pedido-'), 2)
        self.assertLess(len(captured), 75)

        response = self.client.get("/admin/?page=2")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pagina 2 de 2")
        html = response.content.decode()
        self.assertEqual(html.count('<template\n    id="print-macro-'), 1)
        self.assertEqual(html.count('<template\n    id="print-pedido-'), 1)

    def test_admin_configura_ticket_precio_y_password(self):
        self.assertTrue(self.client.login(username="juancarlos", password="TocayosMO2026"))
        producto = Producto.objects.get(nombre="LITRO DE BARBACOA")
        sucursal = SucursalCliente.objects.get(nombre="Aguilas")

        response = self.client.get("/admin/configuracion/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Base de datos")
        self.assertNotContains(response, "pbkdf2_")

        response = self.client.post(
            "/admin/configuracion/",
            data={
                "action": "actualizar_productos",
                f"producto_{producto.id}_present": "1",
                f"producto_{producto.id}_nombre": "LITRO DE BARBACOA",
                f"producto_{producto.id}_ticket": "BARBA",
                f"producto_{producto.id}_unidad": producto.unidad_medida,
                f"producto_{producto.id}_unidad_abreviatura": producto.unidad_abreviatura,
                f"producto_{producto.id}_cantidad_por_precio": str(producto.cantidad_por_precio),
                f"producto_{producto.id}_orden": str(producto.orden),
                f"producto_{producto.id}_activo": "on",
            },
        )
        self.assertRedirects(response, "/admin/configuracion/")

        response = self.client.post(
            "/admin/configuracion/",
            data={
                "action": "actualizar_precios",
                f"precio_{producto.id}_{sucursal.id}": "12.50",
                f"precio_ticket_{producto.id}_{sucursal.id}": "BARBA",
            },
        )
        self.assertRedirects(response, "/admin/configuracion/")
        precio = Precio.objects.get(
            producto=producto,
            sucursal_cliente=sucursal,
            fecha_vigencia__isnull=False,
        )
        self.assertEqual(precio.precio_unitario, Decimal("12.50"))

        response = self.client.post(
            "/admin/configuracion/",
            data={
                "action": "actualizar_sucursales",
                f"sucursal_{sucursal.id}_present": "1",
                f"sucursal_{sucursal.id}_nombre": sucursal.nombre,
                f"sucursal_{sucursal.id}_tipo": sucursal.tipo,
                f"sucursal_{sucursal.id}_username": sucursal.usuario.username,
                f"sucursal_{sucursal.id}_email": sucursal.email,
                f"sucursal_{sucursal.id}_password": "NuevaClave123",
                f"sucursal_{sucursal.id}_activa": "on",
            },
        )
        self.assertRedirects(response, "/admin/configuracion/")

        self.client.logout()
        self.assertFalse(self.client.login(username="aguilas", password="Aguilas8445"))
        self.assertTrue(self.client.login(username="aguilas", password="NuevaClave123"))
        response = self.client.post(
            "/api/pedidos/crear-item/",
            data=json.dumps({"producto_id": producto.id, "cantidad": "2"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total_pedido"], "25.00")
        response = self.client.post("/api/pedidos/confirmar/", content_type="application/json")
        pedido_id = response.json()["pedido_id"]

        self.client.logout()
        self.assertTrue(self.client.login(username="juancarlos", password="TocayosMO2026"))
        response = self.client.get(f"/admin/pedidos/{pedido_id}/imprimir/")
        self.assertContains(response, "BARBA")

    def test_admin_puede_crear_producto_y_usuario(self):
        self.assertTrue(self.client.login(username="juancarlos", password="TocayosMO2026"))
        response = self.client.post(
            "/admin/configuracion/",
            data={
                "action": "crear_producto",
                "nuevo_nombre": "Producto Prueba",
                "nuevo_ticket": "PRUEBA",
                "nuevo_unidad": "LITRO (LT)",
                "nuevo_unidad_abreviatura": "LT",
                "nuevo_cantidad_por_precio": "1.000",
                "nuevo_orden": "7",
            },
        )
        self.assertRedirects(response, "/admin/configuracion/")
        self.assertTrue(
            Producto.objects.filter(nombre="Producto Prueba", nombre_ticket="PRUEBA", activo=True).exists()
        )

        response = self.client.post(
            "/admin/configuracion/",
            data={
                "action": "crear_sucursal",
                "nuevo_sucursal_nombre": "Sucursal Prueba",
                "nuevo_sucursal_tipo": SucursalCliente.Tipo.SUCURSAL,
                "nuevo_sucursal_username": "sucursal_prueba",
                "nuevo_sucursal_password": "ClavePrueba123",
            },
        )
        self.assertRedirects(response, "/admin/configuracion/")
        created_user = User.objects.get(username="sucursal_prueba")
        self.assertTrue(created_user.check_password("ClavePrueba123"))
        self.assertTrue(
            SucursalCliente.objects.filter(nombre="Sucursal Prueba", usuario=created_user).exists()
        )

    def test_admin_levanta_pedido_para_sucursal_sin_limite_horario(self):
        config = Configuracion.get_solo()
        config.hora_inicio_pedidos = time(0, 0)
        config.hora_fin_pedidos = time(0, 1)
        config.save()
        cache.delete(CONFIGURACION_CACHE_KEY)

        sucursal = SucursalCliente.objects.get(nombre="Plaza del Sol")
        producto = Producto.objects.get(nombre="LITRO DE BARBACOA")
        self.assertTrue(self.client.login(username="juancarlos", password="TocayosMO2026"))

        page = self.client.get(f"/admin/pedidos/nuevo/?sucursal={sucursal.id}")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Levantar pedido para")
        self.assertContains(page, "Plaza del Sol")
        self.assertContains(page, "/admin/api/pedidos/crear-item/")

        response = self.client.post(
            "/admin/api/pedidos/crear-item/",
            data=json.dumps(
                {
                    "sucursal_id": sucursal.id,
                    "producto_id": producto.id,
                    "cantidad": "2",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        item_id = data["item_id"]

        response = self.client.post(
            "/admin/api/pedidos/eliminar-item/",
            data=json.dumps({"sucursal_id": sucursal.id, "item_id": item_id}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["pedido"]["items"], [])

        response = self.client.post(
            "/admin/api/pedidos/crear-item/",
            data=json.dumps(
                {
                    "sucursal_id": sucursal.id,
                    "producto_id": producto.id,
                    "cantidad": "2",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])

        response = self.client.post(
            "/admin/api/pedidos/limpiar/",
            data=json.dumps({"sucursal_id": sucursal.id}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["pedido"]["items"], [])

        response = self.client.post(
            "/admin/api/pedidos/crear-item/",
            data=json.dumps(
                {
                    "sucursal_id": sucursal.id,
                    "producto_id": producto.id,
                    "cantidad": "2",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])

        response = self.client.post(
            "/admin/api/pedidos/confirmar/",
            data=json.dumps({"sucursal_id": sucursal.id}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        pedido = Pedido.objects.get(id=response.json()["pedido_id"])
        self.assertEqual(pedido.sucursal_cliente, sucursal)
        self.assertEqual(pedido.estado, Pedido.Estado.CONFIRMADO)

    def test_cinco_pedidos_se_agrupan_y_el_sexto_se_bloquea(self):
        sucursal = SucursalCliente.objects.get(nombre="Plaza del Sol")
        producto = Producto.objects.get(nombre="LITRO DE BARBACOA")
        self.assertTrue(self.client.login(username="juancarlos", password="TocayosMO2026"))

        macropedido_id = None
        for numero in range(1, 6):
            response = self.client.post(
                "/admin/api/pedidos/crear-item/",
                data=json.dumps(
                    {
                        "sucursal_id": sucursal.id,
                        "producto_id": producto.id,
                        "cantidad": str(numero),
                    }
                ),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 200)

            response = self.client.post(
                "/admin/api/pedidos/confirmar/",
                data=json.dumps({"sucursal_id": sucursal.id}),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["pedidos_del_dia"], numero)
            macropedido_id = macropedido_id or data["macropedido_id"]
            self.assertEqual(data["macropedido_id"], macropedido_id)

        macropedido = MacroPedido.objects.get(id=macropedido_id)
        self.assertEqual(macropedido.cantidad_pedidos, 5)
        self.assertEqual(macropedido.pedidos.count(), 5)
        self.assertEqual(
            macropedido.total,
            sum(macropedido.pedidos.values_list("total", flat=True), Decimal("0.00")),
        )

        response = self.client.post(
            "/admin/api/pedidos/crear-item/",
            data=json.dumps(
                {
                    "sucursal_id": sucursal.id,
                    "producto_id": producto.id,
                    "cantidad": "6",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("5 pedidos", response.json()["mensaje"])

        dashboard = self.client.get(f"/admin/?sucursal={sucursal.id}")
        self.assertContains(dashboard, 'aria-label="5 de 5 pedidos realizados"')
        self.assertContains(dashboard, "Pedido 5")
        self.assertContains(dashboard, "15 KG")
        self.assertContains(dashboard, 'class="order-segment segment-5 filled"')

        print_response = self.client.get(f"/admin/macropedidos/{macropedido.id}/imprimir/")
        self.assertEqual(print_response.status_code, 200)
        self.assertContains(print_response, "15 KG")

        page = self.client.get(f"/admin/pedidos/nuevo/?sucursal={sucursal.id}")
        self.assertContains(page, "L&iacute;mite diario alcanzado", html=True)
        self.assertContains(page, 'class="order-segment segment-5 filled"')

    def test_admin_imprime_aguas_del_ultimo_pedido_confirmado_por_sucursal(self):
        fecha_antigua = timezone.now() - timedelta(hours=25)
        fecha_reciente = timezone.now() - timedelta(hours=2)
        fecha_mas_reciente = timezone.now() - timedelta(minutes=20)
        self.crear_pedido_confirmado(
            "Estancia",
            [
                ("AGUA HORCHATA BLANCA 1/2", "99"),
                ("AGUA HORCHATA ROSA LT", "99"),
            ],
            fecha_antigua,
        )
        self.crear_pedido_confirmado(
            "Estancia",
            [
                ("AGUA HORCHATA BLANCA 1/2", "8"),
                ("AGUA HORCHATA ROSA LT", "8"),
            ],
            fecha_reciente,
        )
        self.crear_pedido_confirmado(
            "Estancia",
            [
                ("AGUA HORCHATA BLANCA 1/2", "2"),
                ("AGUA HORCHATA ROSA LT", "3"),
            ],
            fecha_mas_reciente,
        )
        self.crear_pedido_confirmado(
            "Aguilas",
            [
                ("AGUA HORCHATA BLANCA 1/2", "5"),
                ("AGUA JAMAICA LT", "1"),
            ],
            fecha_reciente,
        )
        self.crear_pedido_confirmado(
            "Fortin",
            [("AGUA HORCHATA BLANCA 1/2", "4")],
            fecha_reciente,
        )
        self.crear_pedido_confirmado(
            "Fortin",
            [("AGUA HORCHATA BLANCA 1/2", "9")],
            fecha_mas_reciente,
            estado=Pedido.Estado.ENVIADO,
        )

        self.assertTrue(self.client.login(username="juancarlos", password="TocayosMO2026"))
        dashboard = self.client.get("/admin/")
        self.assertContains(dashboard, "Aguas")
        self.assertContains(dashboard, "Sucursales")
        self.assertContains(dashboard, "data-inline-print")
        self.assertContains(dashboard, 'id="print-aguas"')
        self.assertNotContains(dashboard, 'target="_blank"')
        response = self.client.get("/admin/aguas/imprimir/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "window.print()")
        self.assertContains(response, "size: 72mm 72mm;")
        self.assertContains(
            response,
            "<tr><td>1/B</td><td>10</td><td>5</td><td>13</td><td>28</td></tr>",
            html=True,
        )
        self.assertContains(response, "<td>10</td>", html=True)
        self.assertContains(response, "<td>5</td>", html=True)
        self.assertContains(response, "<td>13</td>", html=True)
        self.assertContains(response, "<td>28</td>", html=True)
        self.assertNotContains(response, "99")
        self.assertContains(response, "<td>LR</td>", html=True)
        self.assertContains(response, "<td>11</td>", html=True)
        self.assertContains(response, "<td>LJ</td>", html=True)
        self.assertContains(response, "<td>1</td>", html=True)

    def test_admin_imprime_sucursales_del_ultimo_pedido_confirmado(self):
        fecha_antigua = timezone.now() - timedelta(hours=25)
        fecha_reciente = timezone.now() - timedelta(hours=2)
        fecha_mas_reciente = timezone.now() - timedelta(minutes=20)
        self.crear_pedido_confirmado(
            "Estancia",
            [
                ("LITRO DE BARBACOA", "99"),
                ("TORTILLA ESPECIAL", "99"),
                ("CONSOMÉ", "99"),
            ],
            fecha_antigua,
        )
        self.crear_pedido_confirmado(
            "Estancia",
            [
                ("LITRO DE BARBACOA", "8"),
                ("TORTILLA ESPECIAL", "8"),
                ("CONSOMÉ", "8"),
            ],
            fecha_reciente,
        )
        self.crear_pedido_confirmado(
            "Estancia",
            [
                ("LITRO DE BARBACOA", "2"),
                ("TORTILLA ESPECIAL", "3"),
                ("CONSOMÉ", "4"),
            ],
            fecha_mas_reciente,
        )
        self.crear_pedido_confirmado(
            "Brot Nueva Galicia",
            [
                ("LITRO DE BARBACOA", "5"),
                ("TORTILLA ESPECIAL", "6"),
            ],
            fecha_reciente,
        )
        self.crear_pedido_confirmado(
            "Santa Anita",
            [("CONSOMÉ", "7")],
            fecha_reciente,
        )
        self.crear_pedido_confirmado(
            "Aguilas",
            [
                ("LITRO DE BARBACOA", "4"),
                ("TORTILLA ESPECIAL", "4"),
                ("CONSOMÉ", "4"),
            ],
            fecha_reciente,
        )
        self.crear_pedido_confirmado(
            "Aguilas",
            [
                ("LITRO DE BARBACOA", "12"),
                ("TORTILLA ESPECIAL", "12"),
                ("CONSOMÉ", "12"),
            ],
            fecha_mas_reciente,
            estado=Pedido.Estado.ENVIADO,
        )

        self.assertTrue(self.client.login(username="juancarlos", password="TocayosMO2026"))
        dashboard = self.client.get("/admin/")
        self.assertContains(dashboard, "Sucursales")
        self.assertContains(dashboard, 'data-print-template-id="print-sucursales"')
        self.assertContains(dashboard, 'data-print-height="96"')
        self.assertContains(dashboard, 'id="print-sucursales"')

        response = self.client.get("/admin/sucursales/imprimir/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "window.print()")
        self.assertContains(response, "size: 72mm 96mm;")
        self.assertContains(response, 'class="report-total-row"')
        self.assertContains(response, "<th>SUCURSAL</th>", html=True)
        self.assertContains(response, "<th>BBQ</th>", html=True)
        self.assertContains(response, "<th>GORDA</th>", html=True)
        self.assertContains(response, "<th>CONSO</th>", html=True)
        self.assertContains(response, "<td>ESTANCIA</td>", html=True)
        self.assertContains(
            response,
            "<tr><td>AGUILAS</td><td>16</td><td>16</td><td>16</td></tr>",
            html=True,
        )
        self.assertContains(response, "<td>BROT NVA G</td>", html=True)
        self.assertContains(response, "<td>STA ANITA</td>", html=True)
        self.assertContains(response, "<td>7</td>", html=True)
        self.assertContains(response, "<td>31</td>", html=True)
        self.assertContains(response, "<td>33</td>", html=True)
        self.assertContains(response, "<td>35</td>", html=True)
        self.assertContains(
            response,
            '<tr class="report-total-row"><td>TOTAL</td><td>31</td><td>33</td><td>35</td></tr>',
            html=True,
        )
        self.assertNotContains(response, "99")

    def test_reporte_omite_macropedido_enviado_y_toma_confirmado_anterior(self):
        from .views import latest_report_macros

        generado = timezone.make_aware(datetime(2026, 8, 15, 12, 0))
        confirmado = self.crear_pedido_confirmado(
            "Aguilas",
            [("LITRO DE BARBACOA", "3")],
            timezone.make_aware(datetime(2026, 8, 14, 18, 0)),
        )
        enviado = self.crear_pedido_confirmado(
            "Aguilas",
            [("LITRO DE BARBACOA", "9")],
            timezone.make_aware(datetime(2026, 8, 15, 10, 0)),
            estado=Pedido.Estado.ENVIADO,
        )

        seleccionados, _, _ = latest_report_macros(["Aguilas"], generated_at=generado)

        self.assertEqual(seleccionados["Aguilas"].id, confirmado.macropedido_id)
        self.assertNotEqual(seleccionados["Aguilas"].id, enviado.macropedido_id)

    def test_admin_datos_muestra_promedios_y_prediccion(self):
        lunes = timezone.make_aware(datetime(2026, 7, 13, 10, 0))
        self.crear_pedido_confirmado(
            "Aguilas",
            [
                ("LITRO DE BARBACOA", "2"),
                ("AGUA JAMAICA LT", "4"),
            ],
            lunes,
        )
        self.crear_pedido_confirmado(
            "Aguilas",
            [("AGUA JAMAICA LT", "2")],
            lunes + timedelta(minutes=30),
        )

        sucursal = SucursalCliente.objects.get(nombre="Aguilas")
        self.assertTrue(self.client.login(username="juancarlos", password="TocayosMO2026"))
        response = self.client.get(f"/admin/datos/?sucursal={sucursal.id}&dia=1")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Datos")
        self.assertContains(response, "Promedio de Lunes")
        self.assertContains(response, "AGUA JAMAICA LT")
        self.assertContains(response, "Ticket promedio")
        self.assertContains(response, "data-inline-print")
        self.assertContains(response, 'data-print-template-id="print-aguas"')
        self.assertContains(response, 'id="print-aguas"')
        self.assertContains(response, 'data-print-template-id="print-sucursales"')
        self.assertContains(response, 'id="print-sucursales"')
        self.assertNotContains(response, 'target="_blank"')

    def test_seed_crea_usuario_debug_admin(self):
        user = User.objects.get(username="ivanprueba")
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.check_password("prueba8989"))
        self.assertTrue(self.client.login(username="ivanprueba", password="prueba8989"))
        response = self.client.get("/admin/configuracion/")
        self.assertEqual(response.status_code, 200)

    def test_producto_inactivo_no_aparece_en_lista_de_pedido(self):
        producto = Producto.objects.get(nombre="LITRO DE BARBACOA")
        self.assertTrue(self.client.login(username="aguilas", password="Aguilas8445"))
        response = self.client.post(
            "/api/pedidos/crear-item/",
            data=json.dumps({"producto_id": producto.id, "cantidad": "1"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        self.client.logout()
        self.assertTrue(self.client.login(username="juancarlos", password="TocayosMO2026"))
        response = self.client.post(
            "/admin/configuracion/",
            data={
                "action": "actualizar_productos",
                f"producto_{producto.id}_present": "1",
                f"producto_{producto.id}_nombre": producto.nombre,
                f"producto_{producto.id}_ticket": producto.nombre_ticket,
                f"producto_{producto.id}_unidad": producto.unidad_medida,
                f"producto_{producto.id}_unidad_abreviatura": producto.unidad_abreviatura,
                f"producto_{producto.id}_cantidad_por_precio": str(producto.cantidad_por_precio),
                f"producto_{producto.id}_orden": str(producto.orden),
            },
        )
        self.assertRedirects(response, "/admin/configuracion/")
        producto.refresh_from_db()
        self.assertFalse(producto.activo)

        self.client.logout()
        self.assertTrue(self.client.login(username="aguilas", password="Aguilas8445"))
        response = self.client.get("/pedidos/")
        self.assertNotContains(response, f'data-product-id="{producto.id}"')
        self.assertContains(response, producto.nombre)

        self.client.logout()
        self.assertTrue(self.client.login(username="fortin", password="Fortin9481"))
        response = self.client.get("/pedidos/")
        self.assertNotContains(response, f'data-product-id="{producto.id}"')

    def test_seed_crea_diez_clientes_demo(self):
        self.assertEqual(SucursalCliente.objects.count(), 10)
        for nombre, _, _ in CLIENTES_DEMO:
            sucursal = SucursalCliente.objects.get(nombre=nombre)
            self.assertTrue(sucursal.usuario.check_password(password_for_cliente(nombre)))
        self.assertTrue(
            SucursalCliente.objects.get(nombre="Eventos Edgar").usuario.check_password(
                "Eventos Edgar4437"
            )
        )
        self.assertTrue(
            SucursalCliente.objects.get(nombre="Eventos MO").usuario.check_password("Eventos MO6924")
        )
        self.assertTrue(
            SucursalCliente.objects.get(nombre="Plaza del Sol").usuario.check_password(
                "Plaza del Sol3186"
            )
        )
        self.assertTrue(
            SucursalCliente.objects.get(nombre="Santa Anita").usuario.check_password("Santa Anita5702")
        )


class RestriccionHorariaTests(TestCase):
    def setUp(self):
        seed_demo_data()

    def _ventana_fuera_de_ahora(self):
        """Regresa (inicio, fin) que garantizadamente NO incluye la hora actual."""

        ahora = timezone.localtime().time()
        if ahora < time(12, 0):
            return time(20, 0), time(23, 0)
        return time(0, 0), time(1, 0)

    def test_confirmar_pedido_fuera_de_horario_retorna_400(self):
        inicio, fin = self._ventana_fuera_de_ahora()
        config = Configuracion.get_solo()
        config.hora_inicio_pedidos = inicio
        config.hora_fin_pedidos = fin
        config.save()
        cache.delete(CONFIGURACION_CACHE_KEY)

        self.assertTrue(self.client.login(username="aguilas", password="Aguilas8445"))
        producto = Producto.objects.get(nombre="LITRO DE BARBACOA")
        self.client.post(
            "/api/pedidos/crear-item/",
            data=json.dumps({"producto_id": producto.id, "cantidad": "1"}),
            content_type="application/json",
        )

        response = self.client.post("/api/pedidos/confirmar/", content_type="application/json")
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertFalse(data["success"])
        self.assertIn("Pedidos cerrados", data["mensaje"])

        pedido = Pedido.objects.get(sucursal_cliente__nombre="Aguilas")
        self.assertEqual(pedido.estado, Pedido.Estado.PENDIENTE)

    def test_login_muestra_pedidos_cerrados_fuera_de_horario(self):
        inicio, fin = self._ventana_fuera_de_ahora()
        config = Configuracion.get_solo()
        config.hora_inicio_pedidos = inicio
        config.hora_fin_pedidos = fin
        config.save()
        cache.delete(CONFIGURACION_CACHE_KEY)

        response = self.client.get("/login/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pedidos cerrados")

    def test_confirmar_pedido_dentro_de_horario_permite(self):
        abrir_horario_completo()
        self.assertTrue(self.client.login(username="aguilas", password="Aguilas8445"))
        producto = Producto.objects.get(nombre="LITRO DE BARBACOA")
        self.client.post(
            "/api/pedidos/crear-item/",
            data=json.dumps({"producto_id": producto.id, "cantidad": "1"}),
            content_type="application/json",
        )
        response = self.client.post("/api/pedidos/confirmar/", content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])

    def test_endpoint_info_horarios_no_requiere_login(self):
        abrir_horario_completo()
        response = self.client.get("/api/horarios/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["dentro_horario"])
        for key in ("hora_inicio", "hora_fin", "hora_actual", "dentro_horario", "mensaje"):
            self.assertIn(key, data)

    def test_admin_actualiza_horarios_desde_panel_configuracion(self):
        self.assertTrue(self.client.login(username="juancarlos", password="TocayosMO2026"))
        response = self.client.post(
            "/admin/configuracion/",
            data={
                "action": "actualizar_configuracion",
                "hora_inicio_pedidos": "09:00",
                "hora_fin_pedidos": "18:00",
                "hora_envio_recordatorio": "15:30",
                "dias_recordatorio": ["1", "2", "3", "4", "5"],
                "email_remitente": "Los Tocayos <tocayos.tacos@gmail.com>",
                "recordatorios_habilitados": "on",
            },
        )
        self.assertRedirects(response, "/admin/configuracion/")

        config = Configuracion.get_solo()
        self.assertEqual(config.hora_inicio_pedidos, time(9, 0))
        self.assertEqual(config.hora_fin_pedidos, time(18, 0))
        self.assertEqual(config.hora_envio_recordatorio, time(15, 30))
        self.assertEqual(config.dias_recordatorio_lista(), [1, 2, 3, 4, 5])
        self.assertTrue(config.recordatorios_habilitados)
        self.assertEqual(config.actualizado_por, "juancarlos")


class EnviarRecordatoriosCommandTests(TestCase):
    def setUp(self):
        seed_demo_data()
        self.sucursal = SucursalCliente.objects.get(nombre="Aguilas")
        self.sucursal.email = "aguilas@example.com"
        self.sucursal.save()

    def test_modo_test_no_envia_correos_reales(self):
        call_command("enviar_recordatorios", test=True, fuerza=True)
        self.assertEqual(len(mail.outbox), 0)

    def test_fuerza_envia_y_registra_log(self):
        call_command("enviar_recordatorios", fuerza=True)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.sucursal.email, mail.outbox[0].to)
        self.assertIn("tocayos.tacos@gmail.com", mail.outbox[0].from_email)
        self.assertIn("tocayos.tacos@gmail.com", mail.outbox[0].body)
        self.assertTrue(
            LogRecordatorio.objects.filter(
                sucursal_cliente=self.sucursal, estado=LogRecordatorio.Estado.ENVIADO
            ).exists()
        )

    def test_sucursal_sin_correo_se_marca_saltada(self):
        otra = SucursalCliente.objects.get(nombre="Fortin")
        self.assertEqual(otra.email, "")
        call_command("enviar_recordatorios", fuerza=True)
        self.assertTrue(
            LogRecordatorio.objects.filter(
                sucursal_cliente=otra, estado=LogRecordatorio.Estado.SALTADO
            ).exists()
        )

    def test_sin_fuerza_respeta_dias_configurados(self):
        config = Configuracion.get_solo()
        hoy_iso = timezone.localdate().isoweekday()
        # Configura un único día distinto al de hoy para forzar el "saltado".
        otro_dia = 1 if hoy_iso != 1 else 2
        config.dias_recordatorio = str(otro_dia)
        config.save()

        call_command("enviar_recordatorios")
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(LogRecordatorio.objects.exists())

    def test_recordatorios_deshabilitados_sin_fuerza_no_envia(self):
        config = Configuracion.get_solo()
        config.recordatorios_habilitados = False
        config.save()

        call_command("enviar_recordatorios")
        self.assertEqual(len(mail.outbox), 0)
