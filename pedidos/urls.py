from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("pedidos/", views.pedidos_view, name="pedidos"),
    path("api/pedidos/crear-item/", views.crear_item, name="api_crear_item"),
    path("api/pedidos/eliminar-item/", views.eliminar_item, name="api_eliminar_item"),
    path("api/pedidos/limpiar/", views.limpiar_pedido, name="api_limpiar_pedido"),
    path("api/pedidos/confirmar/", views.confirmar_pedido, name="api_confirmar_pedido"),
    path("api/horarios/", views.info_horarios, name="info_horarios"),
    path("admin/", views.admin_dashboard, name="admin_dashboard"),
    path("admin/datos/", views.admin_datos, name="admin_datos"),
    path("admin/configuracion/", views.admin_configuracion, name="admin_configuracion"),
    path("admin/aguas/imprimir/", views.imprimir_aguas, name="imprimir_aguas"),
    path("admin/pedidos/<int:pedido_id>/excel/", views.descargar_excel, name="descargar_excel"),
    path(
        "admin/pedidos/<int:pedido_id>/descargar/",
        views.descargar_y_marcar,
        name="descargar_y_marcar",
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
    path("admin/pedidos/<int:pedido_id>/eliminar/", views.eliminar_pedido, name="eliminar_pedido"),
]
