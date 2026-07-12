from django.core.management.base import BaseCommand

from pedidos.seed import seed_demo_data


class Command(BaseCommand):
    help = "Crea datos demo de Los Tocayos: usuarios, productos y precios."

    def handle(self, *args, **options):
        stats = seed_demo_data()
        self.stdout.write(
            self.style.SUCCESS(
                "Demo lista: "
                f"{stats['usuarios']} usuarios, "
                f"{stats['sucursales_clientes']} sucursales/clientes, "
                f"{stats['productos']} productos, "
                f"{stats['precios']} precios."
            )
        )
