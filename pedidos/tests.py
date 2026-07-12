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
        self.assertEqual(sheet["A1"].value, "Barbacoa")
        self.assertEqual(sheet["B1"].value, 2.5)

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

    def test_usuario_no_admin_no_puede_ver_dashboard(self):
        self.assertTrue(self.client.login(username="fortin", password="Fortin"))
        response = self.client.get("/admin/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/pedidos/")

    def test_seed_crea_seis_clientes_demo(self):
        self.assertEqual(SucursalCliente.objects.count(), 6)
