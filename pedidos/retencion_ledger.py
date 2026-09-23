"""Recibos técnicos externos para reconciliar una restauración aislada.

El JSONL no contiene contenido de Pedido, ItemPedido ni nombres de clientes.
Debe custodiarse fuera del VPS junto con su SHA-256; no se importa al arrancar.
"""

import hashlib
import hmac
import json
import os
import re
import stat
import uuid
from collections import Counter
from datetime import timezone as utc_timezone
from pathlib import Path

from django.conf import settings
from django.core.management.color import no_style
from django.db import connection, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import ExportacionRetencion, Pedido, PedidoPurgado, RegistroPurga


FORMATO = "tcys-retencion-ledger"
VERSION = 1
MAX_LINEA_BYTES = 8192
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")


class LedgerError(ValueError):
    """Ledger incompleto, alterado o incompatible con la base restaurada."""


def _instante(valor):
    if valor is None:
        return None
    return valor.astimezone(utc_timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _linea(fila):
    return (
        json.dumps(fila, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _filas_ledger():
    """Obtiene una foto coherente; en PostgreSQL usa snapshot de sólo lectura."""
    transaccion_externa = connection.in_atomic_block
    with transaction.atomic():
        if connection.vendor == "postgresql" and not transaccion_externa:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")

        recibos = list(RegistroPurga.objects.order_by("pk"))
        tombstones = list(PedidoPurgado.objects.order_by("codigo_publico"))
        miembros = Counter(fila.registro_id for fila in tombstones)
        if any(miembros[fila.pk] != fila.numero_pedidos for fila in recibos):
            raise LedgerError("Los tombstones no coinciden con sus recibos de purga.")
        ids_origen = [fila.pedido_id_origen for fila in tombstones]
        if len(ids_origen) != len(set(ids_origen)):
            raise LedgerError("Existen IDs originales duplicados entre tombstones.")
        exportacion_ids = {
            valor
            for valor in (
                *(registro.exportacion_id for registro in recibos),
                *(tombstone.exportacion_id for tombstone in tombstones),
            )
            if valor is not None
        }
        exportaciones = sorted(
            ExportacionRetencion.objects.filter(pk__in=exportacion_ids),
            key=lambda fila: str(fila.pk),
        )
        if len(exportaciones) != len(exportacion_ids):
            raise LedgerError("Una referencia de exportación no existe.")

        filas = [
            {
                "kind": "exportacion",
                "id": str(fila.pk),
                "estado": fila.estado,
                "generada_en": _instante(fila.generada_en),
                "confirmada_en": _instante(fila.confirmada_en),
                "archivo_local_eliminado_en": _instante(fila.archivo_local_eliminado_en),
                "desde_recepcion": _instante(fila.desde_recepcion),
                "hasta_recepcion": _instante(fila.hasta_recepcion),
                "sha256_archivo": fila.sha256_archivo,
                "sha256_contenido": fila.sha256_contenido,
                "numero_pedidos": fila.numero_pedidos,
                "numero_items": fila.numero_items,
                "version_formato": fila.version_formato,
            }
            for fila in exportaciones
        ]
        filas.extend(
            {
                "kind": "recibo",
                "id": fila.pk,
                "ejecutada_en": _instante(fila.ejecutada_en),
                "motivo": fila.motivo,
                "numero_pedidos": fila.numero_pedidos,
                "numero_items": fila.numero_items,
                "numero_macropedidos": fila.numero_macropedidos,
                "numero_eventos": fila.numero_eventos,
                "fecha_confirmacion_min": _instante(fila.fecha_confirmacion_min),
                "fecha_confirmacion_max": _instante(fila.fecha_confirmacion_max),
                "exportacion_id": str(fila.exportacion_id) if fila.exportacion_id else None,
            }
            for fila in recibos
        )
        filas.extend(
            {
                "kind": "tombstone",
                "codigo_publico": str(fila.codigo_publico),
                "pedido_id_origen": fila.pedido_id_origen,
                "motivo": fila.motivo,
                "exportacion_id": str(fila.exportacion_id) if fila.exportacion_id else None,
                "registro_id": fila.registro_id,
            }
            for fila in tombstones
        )
        return filas, {
            "exportaciones": len(exportaciones),
            "recibos": len(recibos),
            "tombstones": len(tombstones),
        }


def exportar_ledger(destino):
    """Crea JSONL y sidecar SHA-256 nuevos, ambos modo 0600, sin sobrescribir."""
    ruta = Path(destino)
    sidecar = Path(f"{ruta}.sha256")
    if not ruta.parent.is_dir() or ruta.exists() or sidecar.exists():
        raise LedgerError("Destino inexistente en directorio existente requerido; no se sobrescribe.")

    filas, conteos = _filas_ledger()
    encabezado = {
        "kind": "header",
        "formato": FORMATO,
        "version": VERSION,
        "generado_en": _instante(timezone.now()),
        "conteos": conteos,
    }
    digest = hashlib.sha256()
    creado_ledger = False
    creado_sidecar = False
    try:
        descriptor = os.open(ruta, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        creado_ledger = True
        with os.fdopen(descriptor, "wb") as archivo:
            for fila in (encabezado, *filas):
                bloque = _linea(fila)
                digest.update(bloque)
                archivo.write(bloque)
            archivo.flush()
            os.fsync(archivo.fileno())

        descriptor_hash = os.open(sidecar, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        creado_sidecar = True
        with os.fdopen(descriptor_hash, "wb") as archivo:
            archivo.write((digest.hexdigest() + "\n").encode("ascii"))
            archivo.flush()
            os.fsync(archivo.fileno())
    except Exception:
        if creado_sidecar:
            sidecar.unlink(missing_ok=True)
        if creado_ledger:
            ruta.unlink(missing_ok=True)
        raise
    return {"sha256": digest.hexdigest(), **conteos}


def _sin_claves_repetidas(pares):
    resultado = {}
    for clave, valor in pares:
        if clave in resultado:
            raise LedgerError("JSON con clave repetida.")
        resultado[clave] = valor
    return resultado


def _claves(fila, esperadas):
    if type(fila) is not dict or set(fila) != set(esperadas):
        raise LedgerError("El ledger contiene campos inesperados o faltantes.")


def _entero(valor, *, minimo=0):
    if type(valor) is not int or valor < minimo:
        raise LedgerError("Entero técnico inválido en ledger.")
    return valor


def _uuid(valor):
    if type(valor) is not str:
        raise LedgerError("UUID técnico inválido en ledger.")
    try:
        convertido = uuid.UUID(valor)
    except ValueError as exc:
        raise LedgerError("UUID técnico inválido en ledger.") from exc
    if str(convertido) != valor:
        raise LedgerError("UUID técnico no canónico en ledger.")
    return convertido


def _uuid_opcional(valor):
    return None if valor is None else _uuid(valor)


def _fecha(valor, *, opcional=False):
    if valor is None and opcional:
        return None
    if type(valor) is not str:
        raise LedgerError("Fecha técnica inválida en ledger.")
    convertido = parse_datetime(valor)
    if convertido is None or timezone.is_naive(convertido) or _instante(convertido) != valor:
        raise LedgerError("Fecha técnica no canónica en ledger.")
    return convertido


def _sha(valor):
    if type(valor) is not str or not HASH_RE.fullmatch(valor):
        raise LedgerError("SHA-256 técnico inválido en ledger.")
    return valor


def _motivo(valor):
    if type(valor) is not str or not 1 <= len(valor) <= 24:
        raise LedgerError("Motivo técnico inválido en ledger.")
    return valor


def _leer_ledger(ruta, esperado):
    if not ruta.is_file() or ruta.is_symlink():
        raise LedgerError("El archivo ledger debe ser regular y no un enlace.")
    if os.name == "posix" and stat.S_IMODE(ruta.stat().st_mode) != 0o600:
        raise LedgerError("El archivo ledger debe tener modo 0600.")
    _sha(esperado)
    digest = hashlib.sha256()
    with ruta.open("rb") as archivo:
        for bloque in iter(lambda: archivo.read(1024 * 1024), b""):
            digest.update(bloque)
    if not hmac.compare_digest(digest.hexdigest(), esperado):
        raise LedgerError("El SHA-256 del ledger no coincide.")

    filas = []
    with ruta.open("rb") as archivo:
        for numero, linea in enumerate(archivo, start=1):
            if len(linea) > MAX_LINEA_BYTES or not linea.endswith(b"\n"):
                raise LedgerError(f"Línea JSONL inválida ({numero}).")
            try:
                fila = json.loads(linea.decode("utf-8"), object_pairs_hook=_sin_claves_repetidas)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise LedgerError(f"JSONL inválido ({numero}).") from exc
            filas.append(fila)
    if not filas:
        raise LedgerError("Ledger vacío.")
    encabezado, *contenido = filas
    _claves(encabezado, {"kind", "formato", "version", "generado_en", "conteos"})
    if encabezado["kind"] != "header" or encabezado["formato"] != FORMATO or encabezado["version"] != VERSION:
        raise LedgerError("Formato/version del ledger incompatibles.")
    _fecha(encabezado["generado_en"])
    _claves(encabezado["conteos"], {"exportaciones", "recibos", "tombstones"})
    for conteo in encabezado["conteos"].values():
        _entero(conteo)

    exportaciones = {}
    recibos = {}
    tombstones = {}
    ids_origen = set()
    for fila in contenido:
        if type(fila) is not dict:
            raise LedgerError("Fila JSONL inválida.")
        tipo = fila.get("kind")
        if tipo == "exportacion":
            _claves(fila, {
                "kind", "id", "estado", "generada_en", "confirmada_en",
                "archivo_local_eliminado_en", "desde_recepcion", "hasta_recepcion",
                "sha256_archivo", "sha256_contenido", "numero_pedidos",
                "numero_items", "version_formato",
            })
            clave = _uuid(fila["id"])
            if clave in exportaciones:
                raise LedgerError("Exportación duplicada en ledger.")
            if fila["estado"] not in ExportacionRetencion.Estado.values:
                raise LedgerError("Estado de exportación inválido.")
            for campo in ("generada_en", "desde_recepcion", "hasta_recepcion"):
                _fecha(fila[campo])
            for campo in ("confirmada_en", "archivo_local_eliminado_en"):
                _fecha(fila[campo], opcional=True)
            for campo in ("sha256_archivo", "sha256_contenido"):
                _sha(fila[campo])
            for campo in ("numero_pedidos", "numero_items"):
                _entero(fila[campo])
            _entero(fila["version_formato"], minimo=1)
            exportaciones[clave] = fila
        elif tipo == "recibo":
            _claves(fila, {
                "kind", "id", "ejecutada_en", "motivo", "numero_pedidos",
                "numero_items", "numero_macropedidos", "numero_eventos",
                "fecha_confirmacion_min", "fecha_confirmacion_max", "exportacion_id",
            })
            clave = _entero(fila["id"], minimo=1)
            if clave in recibos:
                raise LedgerError("Recibo duplicado en ledger.")
            _fecha(fila["ejecutada_en"])
            _motivo(fila["motivo"])
            for campo in ("numero_pedidos", "numero_items", "numero_macropedidos", "numero_eventos"):
                _entero(fila[campo])
            for campo in ("fecha_confirmacion_min", "fecha_confirmacion_max"):
                _fecha(fila[campo], opcional=True)
            _uuid_opcional(fila["exportacion_id"])
            recibos[clave] = fila
        elif tipo == "tombstone":
            _claves(fila, {
                "kind", "codigo_publico", "pedido_id_origen", "motivo",
                "exportacion_id", "registro_id",
            })
            clave = _uuid(fila["codigo_publico"])
            if clave in tombstones:
                raise LedgerError("Tombstone duplicado en ledger.")
            id_origen = _entero(fila["pedido_id_origen"], minimo=1)
            if id_origen in ids_origen:
                raise LedgerError("ID original duplicado en ledger.")
            ids_origen.add(id_origen)
            _motivo(fila["motivo"])
            _uuid_opcional(fila["exportacion_id"])
            _entero(fila["registro_id"], minimo=1)
            tombstones[clave] = fila
        else:
            raise LedgerError("Tipo de registro desconocido en ledger.")

    esperados = encabezado["conteos"]
    if esperados != {
        "exportaciones": len(exportaciones),
        "recibos": len(recibos),
        "tombstones": len(tombstones),
    }:
        raise LedgerError("Los conteos del encabezado no coinciden.")
    miembros = Counter(fila["registro_id"] for fila in tombstones.values())
    if any(clave not in recibos for clave in miembros):
        raise LedgerError("Un tombstone no tiene su recibo de purga.")
    if any(miembros[clave] != fila["numero_pedidos"] for clave, fila in recibos.items()):
        raise LedgerError("El número de tombstones difiere del recibo.")
    if any(
        fila["exportacion_id"] is not None and _uuid(fila["exportacion_id"]) not in exportaciones
        for fila in (*recibos.values(), *tombstones.values())
    ):
        raise LedgerError("Una exportación referenciada falta en el ledger.")
    return exportaciones, recibos, tombstones


def _comparable_exportacion(fila):
    return {
        "kind": "exportacion", "id": str(fila.pk), "estado": fila.estado,
        "generada_en": _instante(fila.generada_en),
        "confirmada_en": _instante(fila.confirmada_en),
        "archivo_local_eliminado_en": _instante(fila.archivo_local_eliminado_en),
        "desde_recepcion": _instante(fila.desde_recepcion),
        "hasta_recepcion": _instante(fila.hasta_recepcion),
        "sha256_archivo": fila.sha256_archivo,
        "sha256_contenido": fila.sha256_contenido,
        "numero_pedidos": fila.numero_pedidos,
        "numero_items": fila.numero_items,
        "version_formato": fila.version_formato,
    }


def _puede_avanzar_exportacion(actual, ledger):
    """Admite sólo GENERADA -> CONFIRMADA con identidad inmutable idéntica."""
    if (
        actual["estado"] != ExportacionRetencion.Estado.GENERADA
        or ledger["estado"] != ExportacionRetencion.Estado.CONFIRMADA
        or actual["confirmada_en"] is not None
        or actual["archivo_local_eliminado_en"] is not None
        or ledger["confirmada_en"] is None
        or ledger["archivo_local_eliminado_en"] is None
    ):
        return False
    campos_inmutables = set(actual) - {
        "estado", "confirmada_en", "archivo_local_eliminado_en"
    }
    return all(actual[campo] == ledger[campo] for campo in campos_inmutables)


def _comparable_recibo(fila):
    return {
        "kind": "recibo", "id": fila.pk,
        "ejecutada_en": _instante(fila.ejecutada_en),
        "motivo": fila.motivo,
        "numero_pedidos": fila.numero_pedidos,
        "numero_items": fila.numero_items,
        "numero_macropedidos": fila.numero_macropedidos,
        "numero_eventos": fila.numero_eventos,
        "fecha_confirmacion_min": _instante(fila.fecha_confirmacion_min),
        "fecha_confirmacion_max": _instante(fila.fecha_confirmacion_max),
        "exportacion_id": str(fila.exportacion_id) if fila.exportacion_id else None,
    }


def _comparable_tombstone(fila):
    return {
        "kind": "tombstone", "codigo_publico": str(fila.codigo_publico),
        "pedido_id_origen": fila.pedido_id_origen,
        "motivo": fila.motivo,
        "exportacion_id": str(fila.exportacion_id) if fila.exportacion_id else None,
        "registro_id": fila.registro_id,
    }


def importar_ledger(origen, *, expected_sha256, aplicar=False):
    """Restaura recibos/tombstones de forma idempotente sólo en base aislada."""
    if not aplicar or getattr(settings, "RETENTION_RESTORE_ISOLATED", False) is not True:
        raise LedgerError("Se requieren --apply y RETENTION_RESTORE_ISOLATED=True.")
    exportaciones, recibos, tombstones = _leer_ledger(Path(origen), expected_sha256)

    with transaction.atomic():
        for clave, fila in exportaciones.items():
            existente = ExportacionRetencion.objects.select_for_update().filter(pk=clave).first()
            if existente is not None:
                actual = _comparable_exportacion(existente)
                if actual != fila:
                    if not _puede_avanzar_exportacion(actual, fila):
                        raise LedgerError("Conflicto con exportación existente.")
                    ExportacionRetencion.objects.filter(pk=clave).update(
                        estado=ExportacionRetencion.Estado.CONFIRMADA,
                        confirmada_en=_fecha(fila["confirmada_en"]),
                        archivo_local_eliminado_en=_fecha(
                            fila["archivo_local_eliminado_en"]
                        ),
                        archivo_local="",
                    )
                continue
            ExportacionRetencion.objects.create(
                id=clave, estado=fila["estado"],
                confirmada_en=_fecha(fila["confirmada_en"], opcional=True),
                archivo_local_eliminado_en=_fecha(
                    fila["archivo_local_eliminado_en"], opcional=True
                ),
                desde_recepcion=_fecha(fila["desde_recepcion"]),
                hasta_recepcion=_fecha(fila["hasta_recepcion"]),
                sha256_archivo=fila["sha256_archivo"],
                sha256_contenido=fila["sha256_contenido"],
                numero_pedidos=fila["numero_pedidos"],
                numero_items=fila["numero_items"],
                version_formato=fila["version_formato"],
                archivo_local="", referencia_confirmacion="",
            )
            ExportacionRetencion.objects.filter(pk=clave).update(
                generada_en=_fecha(fila["generada_en"])
            )

        for clave, fila in recibos.items():
            existente = RegistroPurga.objects.select_for_update().filter(pk=clave).first()
            if existente is not None:
                if _comparable_recibo(existente) != fila:
                    raise LedgerError("Conflicto con recibo de purga existente.")
                continue
            RegistroPurga.objects.create(
                id=clave, motivo=fila["motivo"],
                numero_pedidos=fila["numero_pedidos"],
                numero_items=fila["numero_items"],
                numero_macropedidos=fila["numero_macropedidos"],
                numero_eventos=fila["numero_eventos"],
                fecha_confirmacion_min=_fecha(
                    fila["fecha_confirmacion_min"], opcional=True
                ),
                fecha_confirmacion_max=_fecha(
                    fila["fecha_confirmacion_max"], opcional=True
                ),
                exportacion_id=_uuid_opcional(fila["exportacion_id"]),
            )
            RegistroPurga.objects.filter(pk=clave).update(
                ejecutada_en=_fecha(fila["ejecutada_en"])
            )

        for clave, fila in tombstones.items():
            if PedidoPurgado.objects.filter(
                pedido_id_origen=fila["pedido_id_origen"]
            ).exclude(codigo_publico=clave).exists():
                raise LedgerError("El ID original ya pertenece a otro tombstone.")
            if Pedido.objects.filter(pk=fila["pedido_id_origen"]).exclude(
                codigo_publico=clave
            ).exists():
                raise LedgerError("El ID original ya pertenece a otro pedido.")
            existente = PedidoPurgado.objects.select_for_update().filter(pk=clave).first()
            if existente is not None:
                if _comparable_tombstone(existente) != fila:
                    raise LedgerError("Conflicto con tombstone existente.")
                continue
            PedidoPurgado.objects.create(
                codigo_publico=clave,
                pedido_id_origen=fila["pedido_id_origen"],
                motivo=fila["motivo"],
                exportacion_id=_uuid_opcional(fila["exportacion_id"]),
                registro_id=fila["registro_id"],
            )

        # Los IDs de RegistroPurga se preservan; la secuencia PostgreSQL debe
        # quedar por encima del mayor ID importado antes de un futuro insert.
        if connection.vendor == "postgresql" and recibos:
            with connection.cursor() as cursor:
                for instruccion in connection.ops.sequence_reset_sql(
                    no_style(), [RegistroPurga]
                ):
                    cursor.execute(instruccion)

        reintroducidos = Pedido.objects.filter(codigo_publico__in=tombstones).count()
    return {
        "exportaciones": len(exportaciones),
        "recibos": len(recibos),
        "tombstones": len(tombstones),
        "pedidos_reintroducidos": reintroducidos,
        "requiere_conciliacion": reintroducidos > 0,
    }
