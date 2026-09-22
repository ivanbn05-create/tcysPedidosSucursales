from django.urls import path

from . import views
from .api_pos import pedidos_pos_v1

urlpatterns = [
    path("", views.home, name="home"),
    path("privacidad/", views.privacidad_view, name="privacidad"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("pedidos/", views.pedidos_view, name="pedidos"),
    path("pedidos/historial/", views.historial_pedidos, name="historial_pedidos"),
    path(
        "pedidos/historial/dia/<uuid:codigo_publico>/imprimir/",
        views.imprimir_historial_macropedido,
        name="imprimir_historial_macropedido",
    ),
    path(
        "pedidos/historial/<uuid:codigo_publico>/imprimir/",
        views.imprimir_historial_pedido,
        name="imprimir_historial_pedido",
    ),
    path("api/pedidos/crear-item/", views.crear_item, name="api_crear_item"),
    path("api/pedidos/eliminar-item/", views.eliminar_item, name="api_eliminar_item"),
    path("api/pedidos/limpiar/", views.limpiar_pedido, name="api_limpiar_pedido"),
    path("api/pedidos/confirmar/", views.confirmar_pedido, name="api_confirmar_pedido"),
    path("api/pedidos/log-cliente/", views.log_cliente, name="api_log_cliente"),
    path("api/sesion/heartbeat/", views.heartbeat_sesion, name="api_heartbeat_sesion"),
    path("api/horarios/", views.info_horarios, name="info_horarios"),
    path("api/v1/pos/pedidos/", pedidos_pos_v1, name="api_pos_pedidos_v1"),
    path("admin/", views.admin_dashboard, name="admin_dashboard"),
    path("admin/pedidos/nuevo/", views.admin_crear_pedido_view, name="admin_crear_pedido"),
    path("admin/api/pedidos/crear-item/", views.admin_crear_item, name="admin_api_crear_item"),
    path("admin/api/pedidos/eliminar-item/", views.admin_eliminar_item, name="admin_api_eliminar_item"),
    path("admin/api/pedidos/limpiar/", views.admin_limpiar_pedido, name="admin_api_limpiar_pedido"),
    path("admin/api/pedidos/confirmar/", views.admin_confirmar_pedido, name="admin_api_confirmar_pedido"),
    path("admin/datos/", views.admin_datos, name="admin_datos"),
    path("admin/configuracion/", views.admin_configuracion, name="admin_configuracion"),
    path("admin/diagnostico/", views.admin_diagnostico, name="admin_diagnostico"),
    path("admin/aguas/imprimir/", views.imprimir_aguas, name="imprimir_aguas"),
    path("admin/sucursales/imprimir/", views.imprimir_sucursales, name="imprimir_sucursales"),
    path(
        "admin/macropedidos/<int:macropedido_id>/imprimir/",
        views.imprimir_macropedido,
        name="imprimir_macropedido",
    ),
    path(
        "admin/macropedidos/<int:macropedido_id>/marcar-enviado/",
        views.marcar_macropedido_enviado,
        name="marcar_macropedido_enviado",
    ),
    path(
        "admin/macropedidos/<int:macropedido_id>/revertir-enviado/",
        views.revertir_macropedido_enviado,
        name="revertir_macropedido_enviado",
    ),
    path(
        "admin/macropedidos/<int:macropedido_id>/eliminar/",
        views.eliminar_macropedido,
        name="eliminar_macropedido",
    ),
    path(
        "admin/pedidos/<int:pedido_id>/imprimir/",
        views.imprimir_pedido,
        name="imprimir_pedido",
    ),
    path(
        "admin/pedidos/<int:pedido_id>/marcar-enviado/",
        views.marcar_enviado,
        name="marcar_enviado",
    ),
    path(
        "admin/pedidos/<int:pedido_id>/revertir-enviado/",
        views.revertir_enviado,
        name="revertir_enviado",
    ),
    path("admin/pedidos/<int:pedido_id>/eliminar/", views.eliminar_pedido, name="eliminar_pedido"),
]
