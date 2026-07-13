import json
from decimal import Decimal
from io import BytesIO

from django.test import TestCase
from openpyxl import load_workbook

from .models import Pedido, Producto, SucursalCliente
from .seed import seed_demo_data


class PedidoFlowTests(TestCase):
    def setUp(self):
        seed_demo_data()

    def test_login_crear_confirmar_y_excel(self):
        self.assertTrue(self.client.login(username="aguilas", password="Aguilas"))
        producto = Producto.objects.get(nombre="Barbacoa")

        response = self.client.post(
            "/api/pedidos/crear-item/",
            data=json.dumps({"producto_id": producto.id, "cantidad": "2.5"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["total_pedido"], "2.50")

        response = self.client.post("/api/pedidos/confirmar/", content_type="application/json")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])

        pedido = Pedido.objects.get(id=data["pedido_id"])
        self.assertEqual(pedido.estado, Pedido.Estado.CONFIRMADO)
        self.assertEqual(pedido.total, Decimal("2.50"))

        self.client.logout()
        self.assertTrue(self.client.login(username="admin", password="admin123"))
        response = self.client.get(f"/admin/pedidos/{pedido.id}/excel/")
        self.assertEqual(response.status_code, 200)
        workbook = load_workbook(BytesIO(response.content))
        sheet = workbook.active
        self.assertEqual(sheet.title, "PEDIDOS")
        self.assertIn("A1:C1", [str(range_ref) for range_ref in sheet.merged_cells.ranges])
        self.assertEqual(sheet["A1"].value, "AGUILAS")
        self.assertEqual(sheet["A3"].value, "BARBACOA")
        self.assertEqual(sheet["B3"].value, "2.5")
        self.assertEqual(sheet["C2"].number_format, "d-mmm")
        self.assertAlmostEqual(sheet.column_dimensions["A"].width, 12.42578125)
        self.assertAlmostEqual(sheet.column_dimensions["B"].width, 11.42578125)
        self.assertEqual(sheet.column_dimensions["C"].width, 13)
        self.assertEqual(sheet.row_dimensions[1].height, 21.75)
        self.assertEqual(sheet.row_dimensions[2].height, 16.5)
        self.assertEqual(sheet.row_dimensions[3].height, 26.25)
        self.assertEqual(sheet.row_dimensions[7].height, 23.25)
        self.assertEqual(sheet.page_margins.left, 0)
        self.assertEqual(sheet.page_margins.right, 0)
        self.assertEqual(sheet.page_setup.paperSize, 121)
        self.assertEqual(sheet.page_setup.scale, 90)
        self.assertEqual(sheet.page_setup.horizontalDpi, 203)
        self.assertEqual(sheet.print_area, "'PEDIDOS'!$A$1:$C$33")

        print_response = self.client.get(f"/admin/pedidos/{pedido.id}/imprimir/")
        self.assertEqual(print_response.status_code, 200)
        self.assertContains(print_response, "window.print()")
        self.assertContains(print_response, "size: 58mm 299.49mm;")
        self.assertContains(print_response, "AGUILAS")
        self.assertContains(print_response, "BARBACOA")
        self.assertContains(print_response, "2.5")
        self.assertNotContains(print_response, "$2.50")

    def test_cliente_mayorista_usa_precio_dos_pesos(self):
        self.assertTrue(self.client.login(username="brot_nueva_galicia", password="Brot Nueva Galicia"))
        producto = Producto.objects.get(nombre="Tortilla")
        response = self.client.post(
            "/api/pedidos/crear-item/",
            data=json.dumps({"producto_id": producto.id, "cantidad": "3"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total_pedido"], "6.00")

    def test_agregar_producto_existente_reemplaza_cantidad(self):
        self.assertTrue(self.client.login(username="aguilas", password="Aguilas"))
        producto = Producto.objects.get(nombre="Barbacoa")

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
        self.assertEqual(data["total_pedido"], "3.00")
        self.assertEqual(len(data["pedido"]["items"]), 1)
        self.assertEqual(data["pedido"]["items"][0]["cantidad"], "3.000")

    def test_primer_item_se_muestra_si_habia_pedido_pendiente_vacio(self):
        self.assertTrue(self.client.login(username="aguilas", password="Aguilas"))
        sucursal = SucursalCliente.objects.get(nombre="Aguilas")
        Pedido.objects.create(sucursal_cliente=sucursal, usuario_nombre=sucursal.nombre)
        producto = Producto.objects.get(nombre="Salsa Verde")

        response = self.client.post(
            "/api/pedidos/crear-item/",
            data=json.dumps({"producto_id": producto.id, "cantidad": "1"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total_pedido"], "1.00")
        self.assertEqual(len(data["pedido"]["items"]), 1)
        self.assertEqual(data["pedido"]["items"][0]["producto"], "Salsa Verde")
        self.assertEqual(data["pedido"]["items"][0]["cantidad"], "1.000")

    def test_pantalla_pedidos_no_muestra_precios_unitarios(self):
        self.assertTrue(self.client.login(username="aguilas", password="Aguilas"))
        response = self.client.get("/pedidos/")

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "$1.00")
        self.assertNotContains(response, "precio_unitario")

    def test_usuario_no_admin_no_puede_ver_dashboard(self):
        self.assertTrue(self.client.login(username="fortin", password="Fortin"))
        response = self.client.get("/admin/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/pedidos/")

    def test_seed_crea_seis_clientes_demo(self):
        self.assertEqual(SucursalCliente.objects.count(), 6)
