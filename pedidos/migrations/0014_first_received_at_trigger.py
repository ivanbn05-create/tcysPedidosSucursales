"""Marca la primera inserción en PostgreSQL y protege la marca contra cambios.

Se aplica en la base local después de restaurar el origen. El backfill de filas
históricas NULL es una operación posterior, documentada y supervisada.
"""

from django.conf import settings
from django.db import migrations


def instalar_trigger(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    if not getattr(settings, "RETENTION_LOCAL_DB_CONFIRMED", False):
        raise RuntimeError(
            "La marca de recepción exige RETENTION_LOCAL_DB_CONFIRMED=True en una base VPS local."
        )
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT inet_server_addr()")
        direccion = cursor.fetchone()[0]
    if direccion is not None and str(direccion) not in ("127.0.0.1", "::1"):
        raise RuntimeError("La base PostgreSQL no está en loopback/socket local.")
    for tabla in ("pedidos_pedido", "pedidos_eventocliente"):
        schema_editor.execute(
            f"""
        CREATE OR REPLACE FUNCTION {tabla}_first_received_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                NEW.first_received_at = clock_timestamp();
            ELSIF OLD.first_received_at IS NOT NULL
               AND NEW.first_received_at IS DISTINCT FROM OLD.first_received_at THEN
                RAISE EXCEPTION 'first_received_at is immutable after first assignment';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
        )
        schema_editor.execute(
            f"DROP TRIGGER IF EXISTS {tabla}_first_received_guard_trigger ON {tabla}"
        )
        schema_editor.execute(
            f"""CREATE TRIGGER {tabla}_first_received_guard_trigger
            BEFORE INSERT OR UPDATE ON {tabla}
            FOR EACH ROW EXECUTE FUNCTION {tabla}_first_received_guard()"""
        )


def quitar_trigger(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for tabla in ("pedidos_pedido", "pedidos_eventocliente"):
        schema_editor.execute(
            f"DROP TRIGGER IF EXISTS {tabla}_first_received_guard_trigger ON {tabla}"
        )
        schema_editor.execute(f"DROP FUNCTION IF EXISTS {tabla}_first_received_guard()")


class Migration(migrations.Migration):
    dependencies = [("pedidos", "0013_retencion_exportacion_vps")]

    operations = [migrations.RunPython(instalar_trigger, quitar_trigger)]
