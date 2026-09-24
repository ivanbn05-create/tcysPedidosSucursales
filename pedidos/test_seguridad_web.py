"""Pruebas adversariales de la superficie humana; no dependen de datos reales."""

import json
from datetime import timedelta
from decimal import Decimal

from axes.models import AccessAttempt
from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .models import ItemPedido, MacroPedido, Pedido, Producto, SucursalCliente


@override_settings(
    MFA_ENFORCE=False,
    AUTHENTICATION_BACKENDS=["django.contrib.auth.backends.ModelBackend"],
)
class PermisosWebNegativosTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.usuario_a = User.objects.create_user("sucursal_a", password="ClaveUnicaA-2026")
        cls.usuario_b = User.objects.create_user("sucursal_b", password="ClaveUnicaB-2026")
        cls.admin = User.objects.create_user("matriz", password="ClaveMatriz-2026", is_staff=True)
        cls.impresion = User.objects.create_user("impresion", password="ClavePrint-2026")
        cls.impresion.groups.add(Group.objects.create(name="Operador de impresion"))
        cls.sucursal_a = SucursalCliente.objects.create(
            nombre="Sucursal A", tipo=SucursalCliente.Tipo.SUCURSAL, usuario=cls.usuario_a
        )
        cls.sucursal_b = SucursalCliente.objects.create(
            nombre="Sucursal B", tipo=SucursalCliente.Tipo.SUCURSAL, usuario=cls.usuario_b
        )
        fecha = timezone.now()
        cls.macro_b = MacroPedido.objects.create(
            sucursal_cliente=cls.sucursal_b,
            fecha_pedido=timezone.localdate(fecha),
            ultima_confirmacion=fecha,
        )
        cls.pedido_b = Pedido.objects.create(
            sucursal_cliente=cls.sucursal_b,
            macropedido=cls.macro_b,
            usuario_nombre="Sucursal B",
            fecha_confirmacion=fecha,
            estado=Pedido.Estado.CONFIRMADO,
        )
        cls.producto = Producto.objects.create(nombre="Producto de prueba")
        cls.borrador_b = Pedido.objects.create(
            sucursal_cliente=cls.sucursal_b,
            usuario_nombre="Sucursal B",
            estado=Pedido.Estado.PENDIENTE,
        )
        cls.item_b = ItemPedido.objects.create(
            pedido=cls.borrador_b,
            producto=cls.producto,
            cantidad=Decimal("1.000"),
            precio_unitario=Decimal("1.00"),
        )

    def test_cambiar_uuid_o_id_no_revela_ticket_ni_modifica_item_ajeno(self):
        self.client.force_login(self.usuario_a)
        for ruta in (
            f"/pedidos/historial/{self.pedido_b.codigo_publico}/imprimir/",
            f"/pedidos/historial/dia/{self.macro_b.codigo_publico}/imprimir/",
        ):
            self.assertEqual(self.client.get(ruta).status_code, 404)
        respuesta = self.client.post(
            "/api/pedidos/eliminar-item/",
            data=json.dumps({"item_id": self.item_b.pk}),
            content_type="application/json",
        )
        self.assertEqual(respuesta.status_code, 404)
        self.assertTrue(ItemPedido.objects.filter(pk=self.item_b.pk).exists())

    def test_rol_sucursal_e_impresion_no_pueden_modificar_admin(self):
        for usuario in (self.usuario_a, self.impresion):
            with self.subTest(usuario=usuario.username):
                client = Client()
                client.force_login(usuario)
                respuesta = client.post(f"/admin/pedidos/{self.pedido_b.pk}/eliminar/")
                self.assertEqual(respuesta.status_code, 302)
                self.assertFalse(Pedido.objects.get(pk=self.pedido_b.pk).eliminado)
                self.assertNotEqual(client.get("/admin/configuracion/").status_code, 200)
        self.assertEqual(self.client.get("/admin/pedidos/999/descargar/").status_code, 404)
        self.assertEqual(self.client.get("/admin/exportaciones/1/confirmar/").status_code, 404)
        self.assertEqual(self.client.post("/admin/retencion/1/purgar/").status_code, 404)

    def test_get_no_borra_ni_confirma(self):
        self.client.force_login(self.admin)
        rutas = (
            f"/admin/pedidos/{self.pedido_b.pk}/eliminar/",
            f"/admin/pedidos/{self.pedido_b.pk}/marcar-enviado/",
            f"/admin/pedidos/{self.pedido_b.pk}/revertir-enviado/",
            f"/admin/macropedidos/{self.macro_b.pk}/eliminar/",
            f"/admin/macropedidos/{self.macro_b.pk}/marcar-enviado/",
            f"/admin/macropedidos/{self.macro_b.pk}/revertir-enviado/",
            "/api/pedidos/confirmar/",
        )
        for ruta in rutas:
            with self.subTest(ruta=ruta):
                self.assertEqual(self.client.get(ruta).status_code, 405)
        self.pedido_b.refresh_from_db()
        self.macro_b.refresh_from_db()
        self.assertFalse(self.pedido_b.eliminado)
        self.assertFalse(self.macro_b.eliminado)
        self.assertEqual(self.pedido_b.estado, Pedido.Estado.CONFIRMADO)
        self.assertEqual(self.macro_b.estado, MacroPedido.Estado.CONFIRMADO)

    def test_logout_get_no_cierra_sesion(self):
        self.client.force_login(self.usuario_a)
        self.assertEqual(self.client.get("/logout/").status_code, 405)
        self.assertEqual(self.client.get("/pedidos/historial/").status_code, 200)

    def test_csrf_protege_post_de_admin_y_logout(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post(f"/admin/pedidos/{self.pedido_b.pk}/eliminar/").status_code, 403)
        self.assertEqual(client.post("/logout/").status_code, 403)
        self.assertFalse(Pedido.objects.get(pk=self.pedido_b.pk).eliminado)
        self.assertEqual(client.get("/admin/").status_code, 200)

    def test_diagnostico_con_datos_tecnicos_solo_superusuario(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get("/admin/diagnostico/").status_code, 403)
        self.admin.is_superuser = True
        self.admin.save(update_fields=["is_superuser"])
        self.assertEqual(self.client.get("/admin/diagnostico/").status_code, 200)

    def test_login_ignora_redireccion_externa_y_reset_publico_no_existe(self):
        respuesta = self.client.post(
            "/login/?next=https://ejemplo-invalido.invalid/",
            {"username": "sucursal_a", "password": "ClaveUnicaA-2026"},
        )
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(respuesta.url, "/pedidos/")
        self.assertEqual(self.client.get("/password-reset/").status_code, 404)
        # El catch-all de Django Admin redirige al índice; no ofrece reset público.
        reset_admin = self.client.get("/django-admin/password_reset/")
        self.assertEqual(reset_admin.status_code, 302)
        self.assertTrue(reset_admin.url.startswith("/django-admin/"))

    @override_settings(IS_PRODUCTION=True)
    def test_seed_demo_rechazado_en_produccion(self):
        with self.assertRaisesMessage(CommandError, "prohibido"):
            call_command("seed_demo")


class LimitacionPasswordTests(TestCase):
    def test_fuerza_bruta_bloquea_y_cooldown_permite_recuperar(self):
        User.objects.create_user("victima", password="ClaveVictima-2026")
        client = Client(REMOTE_ADDR="192.0.2.10")
        for _ in range(5):
            respuesta = client.post(
                "/login/", {"username": "victima", "password": "incorrecta"}
            )
        self.assertEqual(respuesta.status_code, 429)
        bloqueada = client.post(
            "/login/", {"username": "victima", "password": "ClaveVictima-2026"}
        )
        self.assertEqual(bloqueada.status_code, 429)
        self.assertNotIn("_auth_user_id", client.session)

        AccessAttempt.objects.update(attempt_time=timezone.now() - timedelta(minutes=16))
        recuperada = client.post(
            "/login/", {"username": "victima", "password": "ClaveVictima-2026"}
        )
        self.assertEqual(recuperada.status_code, 302)
        self.assertIn("_auth_user_id", client.session)
