from django.apps import AppConfig


class PedidosConfig(AppConfig):
    name = 'pedidos'

    def ready(self):
        from . import signals  # noqa: F401
