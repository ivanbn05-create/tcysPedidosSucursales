"""Exportación confirmada y selección de retención; nunca se ejecuta al arrancar."""

import hashlib
import hmac
import json
import os
import stat
import uuid
import zipfile
from dataclasses import asdict, dataclass
from datetime import timedelta, timezone as utc_timezone
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    EventoCliente,
    ExportacionRetencion,
    MacroPedido,
    Pedido,
    PedidoEnExportacion,
    PedidoPurgado,
    RegistroPurga,
)


DIAS_RETENCION = 30
VERSION_EXPORTACION = 1
ESTADOS_OPERATIVAMENTE_ABIERTOS = {
    Pedido.Estado.PENDIENTE,
    Pedido.Estado.CONFIRMADO,
    Pedido.Estado.ENVIADO,
}


def _instante(valor):
    if valor is None:
        return None
    return valor.astimezone(utc_timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _json_canonico(valor):
    return json.dumps(valor, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _sha256(contenido):
    return hashlib.sha256(contenido).hexdigest()


def _sha256_archivo(ruta):
    digest = hashlib.sha256()
    with open(ruta, "rb") as archivo:
        for bloque in iter(lambda: archivo.read(1024 * 1024), b""):
            digest.update(bloque)
    return digest.hexdigest()


def _pedidos_con_relaciones(queryset):
    return queryset.select_related("sucursal_cliente", "macropedido").prefetch_related(
        "items__producto"
    )


def _eventos_referencian_pedidos(ids):
    # El JSON de auditoría puede contener el ID como número o como texto.
    ids = sorted(set(ids))
    return EventoCliente.objects.filter(
        Q(detalle__pedido_id__in=ids)
        | Q(detalle__pedido_id__in=[str(valor) for valor in ids])
    )


def contenido_pedido(pedido):
    """Instantánea para archivo y firma; no se guarda en tablas de comprobantes."""

    macro = pedido.macropedido
    return {
        "id": pedido.pk,
        "codigo_publico": str(pedido.codigo_publico),
        "sucursal": {
            "id": pedido.sucursal_cliente_id,
            "nombre": pedido.sucursal_cliente.nombre,
            "tipo": pedido.sucursal_cliente.tipo,
        },
        "macropedido": (
            {
                "id": macro.pk,
                "codigo_publico": str(macro.codigo_publico),
                "fecha_pedido": macro.fecha_pedido.isoformat(),
            }
            if macro
            else None
        ),
        "usuario_nombre": pedido.usuario_nombre,
        "fecha_creacion": _instante(pedido.fecha_creacion),
        "fecha_confirmacion": _instante(pedido.fecha_confirmacion),
        "first_received_at": _instante(pedido.first_received_at),
        "estado": pedido.estado,
        "eliminado": pedido.eliminado,
        "total": format(pedido.total, "f"),
        "items": [
            {
                "id": item.pk,
                "producto_id": item.producto_id,
                "producto_nombre": item.producto.nombre,
                "producto_nombre_ticket": item.producto.nombre_ticket,
                "unidad_medida": item.producto.unidad_medida,
                "unidad_abreviatura": item.producto.unidad_abreviatura,
                "cantidad_por_precio": format(item.producto.cantidad_por_precio, "f"),
                "cantidad": format(item.cantidad, "f"),
                "precio_unitario": format(item.precio_unitario, "f"),
                "subtotal": format(item.subtotal, "f"),
            }
            for item in sorted(pedido.items.all(), key=lambda fila: fila.pk)
        ],
    }


def firma_pedido(pedido):
    return _sha256(_json_canonico(contenido_pedido(pedido)))


def _validar_ventana(desde, hasta):
    if timezone.is_naive(desde) or timezone.is_naive(hasta) or hasta <= desde:
        raise ValueError("La ventana de recepción debe ser consciente de zona y creciente.")


def _validar_destino_privado(destino):
    prohibidos = [settings.BASE_DIR, getattr(settings, "STATIC_ROOT", None)]
    prohibidos.append(getattr(settings, "MEDIA_ROOT", None))
    for directorio in getattr(settings, "STATICFILES_DIRS", ()):
        # Django admite tanto rutas simples como tuplas (prefijo, ruta).
        prohibidos.append(directorio[1] if isinstance(directorio, (tuple, list)) else directorio)
    for base in prohibidos:
        if base and destino.is_relative_to(Path(base).resolve(strict=False)):
            raise ValueError("El destino debe estar fuera del release y de directorios públicos.")
    if os.name == "posix":
        # El ZIP 0600 no compensa un directorio público o controlado por otro usuario.
        directorio = destino.parent.stat()
        if directorio.st_uid != os.geteuid() or stat.S_IMODE(directorio.st_mode) & 0o077:
            raise ValueError("El directorio de exportación debe ser propio y privado (0700).")


def generar_exportacion(*, desde, hasta, destino, limite=500, id_desde=0, id_hasta=None):
    """Crea ZIP 0600 y ticket GENERADA; no confirma descarga ni habilita purga."""

    marcador = {"archivo_creado": False, "destino": None}
    try:
        return _generar_exportacion_transaccional(
            desde=desde, hasta=hasta, destino=destino, limite=limite,
            id_desde=id_desde, id_hasta=id_hasta, marcador=marcador,
        )
    except Exception:
        # Cubre también un fallo de COMMIT, fuera del bloque interior de escritura.
        if marcador["archivo_creado"]:
            marcador["destino"].unlink(missing_ok=True)
        raise


def _generar_exportacion_transaccional(
    *, desde, hasta, destino, limite, id_desde, id_hasta, marcador
):

    _validar_ventana(desde, hasta)
    if not 1 <= limite <= 5000:
        raise ValueError("limite debe estar entre 1 y 5000.")
    if id_desde < 0 or (id_hasta is not None and id_hasta <= id_desde):
        raise ValueError("El rango de IDs debe ser creciente y no negativo.")
    destino = Path(destino).resolve(strict=False)
    if len(str(destino)) > 500:
        raise ValueError("La ruta del archivo es demasiado larga.")
    if destino.exists() or not destino.parent.is_dir():
        raise ValueError("El destino debe ser un archivo nuevo en un directorio existente.")
    _validar_destino_privado(destino)

    with transaction.atomic():
        consulta = (
            Pedido.objects.select_for_update()
            .filter(
                first_received_at__gte=desde,
                first_received_at__lt=hasta,
                id__gte=id_desde,
            )
            .order_by("first_received_at", "id")
        )
        if id_hasta is not None:
            consulta = consulta.filter(id__lt=id_hasta)
        pedidos = list(_pedidos_con_relaciones(consulta[: limite + 1]))
        if len(pedidos) > limite:
            raise ValueError("La ventana excede el limite; divídela antes de exportar.")
        if not pedidos:
            raise ValueError("No hay pedidos con marca de recepción en esa ventana.")

        filas = [contenido_pedido(pedido) for pedido in pedidos]
        lineas = b"".join(_json_canonico(fila) + b"\n" for fila in filas)
        contenido_sha = _sha256(lineas)
        numero_items = sum(len(fila["items"]) for fila in filas)
        lote_id = uuid.uuid4()
        manifest = {
            "formato": "tcys-pedidos-retencion",
            "version": VERSION_EXPORTACION,
            "lote_id": str(lote_id),
            "generado_en": _instante(timezone.now()),
            "desde_recepcion_inclusivo": _instante(desde),
            "hasta_recepcion_exclusivo": _instante(hasta),
            "pedido_id_desde_inclusivo": id_desde,
            "pedido_id_hasta_exclusivo": id_hasta,
            "numero_pedidos": len(filas),
            "numero_items": numero_items,
            "pedido_ids": [fila["id"] for fila in filas],
            "sha256_pedidos_jsonl": contenido_sha,
        }

        descriptor = os.open(destino, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        marcador["archivo_creado"] = True
        marcador["destino"] = destino
        try:
            with os.fdopen(descriptor, "wb") as archivo:
                with zipfile.ZipFile(archivo, "w", compression=zipfile.ZIP_DEFLATED) as zip_out:
                    zip_out.writestr("manifest.json", _json_canonico(manifest) + b"\n")
                    zip_out.writestr("pedidos.jsonl", lineas)
            archivo_sha = _sha256_archivo(destino)
            lote = ExportacionRetencion.objects.create(
                id=lote_id,
                desde_recepcion=desde,
                hasta_recepcion=hasta,
                sha256_archivo=archivo_sha,
                sha256_contenido=contenido_sha,
                archivo_local=str(destino),
                numero_pedidos=len(filas),
                numero_items=numero_items,
                version_formato=VERSION_EXPORTACION,
            )
            PedidoEnExportacion.objects.bulk_create(
                [
                    PedidoEnExportacion(
                        exportacion=lote,
                        pedido=pedido,
                        sha256_contenido=_sha256(_json_canonico(fila)),
                    )
                    for pedido, fila in zip(pedidos, filas)
                ]
            )
        except Exception:
            destino.unlink(missing_ok=True)
            raise

    return lote


def confirmar_exportacion(*, lote_id, archivo_local, sha256_destino, referencia):
    """Confirma el hash del destino; purga sólo tras limpiar el ZIP local."""

    if not referencia or len(referencia) > 120:
        raise ValueError("Se requiere una referencia de confirmación de 1 a 120 caracteres.")
    if len(sha256_destino) != 64 or any(c not in "0123456789abcdef" for c in sha256_destino):
        raise ValueError("Se requiere el SHA-256 hexadecimal calculado en destino.")
    archivo_local = Path(archivo_local)
    if archivo_local.is_symlink() or not archivo_local.is_file():
        raise ValueError("El archivo local del lote debe existir y no ser un enlace.")

    obsoleto = False
    with transaction.atomic():
        lote = ExportacionRetencion.objects.select_for_update().get(pk=lote_id)
        if lote.estado != ExportacionRetencion.Estado.GENERADA:
            raise ValueError("El lote ya no está en estado GENERADA.")
        if archivo_local.resolve() != Path(lote.archivo_local):
            raise ValueError("El archivo no corresponde a la ruta del lote.")
        hash_local = _sha256_archivo(archivo_local)
        if not (
            hmac.compare_digest(hash_local, lote.sha256_archivo)
            and hmac.compare_digest(hash_local, sha256_destino)
        ):
            raise ValueError("El SHA-256 local/destino no coincide con el ticket.")

        miembros = list(lote.miembros.select_related("pedido").order_by("pedido_id"))
        pedidos = {
            pedido.pk: pedido
            for pedido in _pedidos_con_relaciones(
                Pedido.objects.select_for_update().filter(pk__in=[m.pedido_id for m in miembros])
            )
        }
        if len(pedidos) != lote.numero_pedidos or any(
            firma_pedido(pedidos[miembro.pedido_id]) != miembro.sha256_contenido
            for miembro in miembros
        ):
            lote.estado = ExportacionRetencion.Estado.INVALIDADA
            lote.save(update_fields=["estado"])
            obsoleto = True
        else:
            lote.estado = ExportacionRetencion.Estado.CONFIRMADA
            lote.confirmada_en = timezone.now()
            lote.referencia_confirmacion = referencia
            lote.save(update_fields=["estado", "confirmada_en", "referencia_confirmacion"])
    if obsoleto:
        raise ValueError("Un pedido cambió o falta desde la exportación; genera otro lote.")
    return finalizar_limpieza_exportacion(lote_id=lote_id)


def finalizar_limpieza_exportacion(*, lote_id, permitir_ausente=False):
    """Cierra fase 2. Una caída tras unlink deja el lote confirmado, no purgable."""

    with transaction.atomic():
        lote = ExportacionRetencion.objects.select_for_update().get(pk=lote_id)
        if lote.estado != ExportacionRetencion.Estado.CONFIRMADA:
            raise ValueError("Sólo un lote confirmado puede cerrar su archivo local.")
        if lote.archivo_local_eliminado_en is not None:
            return lote
        ruta = Path(lote.archivo_local)
        if not lote.archivo_local or ruta.is_symlink():
            raise ValueError("La ruta local del lote es inválida.")
        if ruta.exists():
            if not ruta.is_file() or not hmac.compare_digest(
                _sha256_archivo(ruta), lote.sha256_archivo
            ):
                raise ValueError("El archivo local cambió; limpieza manual requerida.")
            ruta.unlink()
        elif not permitir_ausente:
            raise ValueError("El archivo local falta; verifica su ausencia antes de cerrar.")
        lote.archivo_local_eliminado_en = timezone.now()
        lote.archivo_local = ""
        lote.save(update_fields=["archivo_local_eliminado_en", "archivo_local"])
        return lote


def invalidar_exportacion_obsoleta(*, lote_id, ahora=None, aplicar=False, permitir_ausente=False):
    """Retira manualmente un ZIP no confirmado; jamás lo vuelve elegible para purga."""

    ahora = ahora or timezone.now()
    with transaction.atomic():
        lote = ExportacionRetencion.objects.select_for_update().get(pk=lote_id)
        if lote.estado == ExportacionRetencion.Estado.CONFIRMADA:
            raise ValueError("Un lote confirmado requiere finalizar_limpieza_exportacion.")
        if (lote.estado == ExportacionRetencion.Estado.GENERADA
                and lote.generada_en > ahora - timedelta(hours=24)):
            raise ValueError("Un lote no confirmado debe superar 24 horas antes de invalidarse.")
        if not aplicar:
            return lote
        if not getattr(settings, "RETENTION_EXPORT_CLEANUP_ENABLED", False):
            raise RuntimeError("La limpieza real de exports está deshabilitada.")
        if lote.estado == ExportacionRetencion.Estado.GENERADA:
            lote.estado = ExportacionRetencion.Estado.INVALIDADA
            lote.save(update_fields=["estado"])

    # Si falla el borrado sigue INVALIDADA: no se habilita purga y se puede reintentar.
    with transaction.atomic():
        lote = ExportacionRetencion.objects.select_for_update().get(pk=lote_id)
        if lote.archivo_local_eliminado_en is not None:
            return lote
        ruta = Path(lote.archivo_local)
        if not lote.archivo_local or ruta.is_symlink():
            raise ValueError("La ruta local del lote es inválida.")
        if ruta.exists():
            if not ruta.is_file() or not hmac.compare_digest(
                _sha256_archivo(ruta), lote.sha256_archivo
            ):
                raise ValueError("El ZIP local cambió; revisión manual requerida.")
            ruta.unlink()
        elif not permitir_ausente:
            raise ValueError("El ZIP falta; verifica ausencia antes de cerrar.")
        lote.archivo_local_eliminado_en = timezone.now()
        lote.archivo_local = ""
        lote.save(update_fields=["archivo_local_eliminado_en", "archivo_local"])
        return lote


@dataclass(frozen=True)
class PlanPurga:
    evaluado_en: str
    vencimiento: str
    pedidos_por_edad: tuple[int, ...]
    pedidos_por_exportacion: tuple[int, ...]
    pedidos_bloqueados_abiertos: tuple[int, ...]
    pedidos_sin_marca: int
    eventos_sin_marca: int
    pedidos_exportacion_no_confirmada: int
    pedidos_exportacion_obsoleta: tuple[int, ...]
    items_dependientes: int
    macropedidos_vacios: tuple[int, ...]
    eventos_vencidos: int
    eventos_referencian_pedidos: int

    def como_dict(self):
        return asdict(self)


def planificar_purga(*, ahora=None):
    """Plan sin escrituras: edad y descarga confirmada, con bloqueos visibles."""

    ahora = ahora or timezone.now()
    if timezone.is_naive(ahora):
        raise ValueError("ahora debe tener zona horaria.")
    vencimiento = ahora - timedelta(days=DIAS_RETENCION)

    confirmados = {}
    membresias = PedidoEnExportacion.objects.filter(
        exportacion__estado=ExportacionRetencion.Estado.CONFIRMADA,
        exportacion__archivo_local_eliminado_en__isnull=False,
    ).select_related("exportacion")
    for miembro in membresias.order_by("exportacion__confirmada_en", "id"):
        confirmados.setdefault(miembro.pedido_id, []).append(miembro)

    candidatos = _pedidos_con_relaciones(
        Pedido.objects.filter(
            Q(first_received_at__lte=vencimiento) | Q(pk__in=confirmados)
        ).order_by("id")
    )
    por_edad = []
    por_exportacion = []
    abiertos = []
    obsoletos = []
    macros_en_candidatos = set()
    items = 0
    for pedido in candidatos:
        vencido = pedido.first_received_at is not None and pedido.first_received_at <= vencimiento
        miembros = confirmados.get(pedido.pk, ())
        exportacion_valida = bool(miembros) and any(
            firma_pedido(pedido) == miembro.sha256_contenido for miembro in miembros
        )
        if miembros and not exportacion_valida:
            obsoletos.append(pedido.pk)
        if not vencido and not exportacion_valida:
            continue
        if not pedido.eliminado and pedido.estado in ESTADOS_OPERATIVAMENTE_ABIERTOS:
            abiertos.append(pedido.pk)
            continue
        if vencido:
            por_edad.append(pedido.pk)
        else:
            por_exportacion.append(pedido.pk)
        items += len(pedido.items.all())
        if pedido.macropedido_id:
            macros_en_candidatos.add(pedido.macropedido_id)

    seleccionados = set(por_edad) | set(por_exportacion)
    macros_vacios = tuple(
        sorted(
            macro_id
            for macro_id in macros_en_candidatos
            if not Pedido.objects.filter(macropedido_id=macro_id).exclude(pk__in=seleccionados).exists()
        )
    )
    eventos_referencian = _eventos_referencian_pedidos(seleccionados).count() if seleccionados else 0
    return PlanPurga(
        evaluado_en=_instante(ahora),
        vencimiento=_instante(vencimiento),
        pedidos_por_edad=tuple(por_edad),
        pedidos_por_exportacion=tuple(por_exportacion),
        pedidos_bloqueados_abiertos=tuple(abiertos),
        pedidos_sin_marca=Pedido.objects.filter(first_received_at__isnull=True).count(),
        eventos_sin_marca=EventoCliente.objects.filter(first_received_at__isnull=True).count(),
        pedidos_exportacion_no_confirmada=PedidoEnExportacion.objects.filter(
            exportacion__estado=ExportacionRetencion.Estado.GENERADA
        ).values("pedido_id").distinct().count(),
        pedidos_exportacion_obsoleta=tuple(obsoletos),
        items_dependientes=items,
        macropedidos_vacios=macros_vacios,
        eventos_vencidos=EventoCliente.objects.filter(first_received_at__lte=vencimiento).count(),
        eventos_referencian_pedidos=eventos_referencian,
    )


def aplicar_purga(*, ahora=None):
    """Borrado manual atómico; exige configuración y cero pedidos abiertos vencidos."""

    if not getattr(settings, "RETENTION_PURGE_ENABLED", False):
        raise RuntimeError("La purga real está deshabilitada por configuración.")
    with transaction.atomic():
        # Serializa la purga y los cambios de pedidos durante la selección.
        list(Pedido.objects.select_for_update().order_by("id").values_list("id", flat=True))
        plan = planificar_purga(ahora=ahora)
        if plan.pedidos_bloqueados_abiertos:
            raise RuntimeError("Hay pedidos operativamente abiertos elegibles; requiere decisión del dueño.")
        ids = set(plan.pedidos_por_edad) | set(plan.pedidos_por_exportacion)
        instante = ahora or timezone.now()
        eventos = EventoCliente.objects.filter(first_received_at__lte=instante - timedelta(days=DIAS_RETENCION))
        if ids:
            eventos = eventos | _eventos_referencian_pedidos(ids)
        evento_ids = set(eventos.values_list("pk", flat=True))
        numero_eventos = len(evento_ids)
        EventoCliente.objects.filter(pk__in=evento_ids).delete()
        pedidos = list(Pedido.objects.filter(pk__in=ids).order_by("id"))
        fechas = [pedido.fecha_confirmacion for pedido in pedidos if pedido.fecha_confirmacion]
        macro_ids = {pedido.macropedido_id for pedido in pedidos if pedido.macropedido_id}
        macros_vacios = list(plan.macropedidos_vacios)
        registro = None
        if ids or numero_eventos:
            registro = RegistroPurga.objects.create(
                motivo="mixta" if plan.pedidos_por_edad and plan.pedidos_por_exportacion else (
                    "antiguedad" if plan.pedidos_por_edad or numero_eventos else "exportacion"
                ),
                numero_pedidos=len(ids),
                numero_items=plan.items_dependientes,
                numero_macropedidos=len(macros_vacios),
                numero_eventos=numero_eventos,
                fecha_confirmacion_min=min(fechas) if fechas else None,
                fecha_confirmacion_max=max(fechas) if fechas else None,
            )
        if registro and pedidos:
            exportaciones_validas = {}
            if plan.pedidos_por_exportacion:
                miembros = PedidoEnExportacion.objects.filter(
                    pedido_id__in=plan.pedidos_por_exportacion,
                    exportacion__estado=ExportacionRetencion.Estado.CONFIRMADA,
                    exportacion__archivo_local_eliminado_en__isnull=False,
                ).select_related("exportacion")
                por_pedido = {pedido.pk: pedido for pedido in pedidos}
                for miembro in miembros.order_by("exportacion__confirmada_en", "id"):
                    if (miembro.pedido_id not in exportaciones_validas
                            and firma_pedido(por_pedido[miembro.pedido_id]) == miembro.sha256_contenido):
                        exportaciones_validas[miembro.pedido_id] = miembro.exportacion
                if set(exportaciones_validas) != set(plan.pedidos_por_exportacion):
                    raise RuntimeError("Cambió un pedido desde la selección de exportación.")
            PedidoPurgado.objects.bulk_create(
                [
                    PedidoPurgado(
                        codigo_publico=pedido.codigo_publico,
                        pedido_id_origen=pedido.pk,
                        sucursal_cliente_id=pedido.sucursal_cliente_id,
                        fecha_confirmacion=pedido.fecha_confirmacion,
                        motivo=("antiguedad" if pedido.pk in plan.pedidos_por_edad else "exportacion"),
                        exportacion=exportaciones_validas.get(pedido.pk),
                        registro=registro,
                    )
                    for pedido in pedidos
                ]
            )
        Pedido.objects.filter(pk__in=ids).delete()
        MacroPedido.objects.filter(pk__in=macros_vacios).delete()
        for macro_id in sorted(macro_ids - set(macros_vacios)):
            MacroPedido.objects.get(pk=macro_id).recalcular_resumen()
        return plan


def conciliar_restauracion(*, aplicar=False):
    """Detecta y elimina reintroducciones sólo en una restauración aislada."""

    codigos = PedidoPurgado.objects.values_list("codigo_publico", flat=True)
    restaurados = Pedido.objects.filter(codigo_publico__in=codigos)
    ids = list(restaurados.values_list("pk", flat=True))
    resumen = {"pedidos_reintroducidos": len(ids)}
    if not aplicar or not ids:
        return resumen
    if not getattr(settings, "RETENTION_RESTORE_ISOLATED", False):
        raise RuntimeError("La reconciliación real sólo admite una base restaurada aislada.")
    with transaction.atomic():
        pedidos = list(Pedido.objects.select_for_update().filter(pk__in=ids))
        macro_ids = {pedido.macropedido_id for pedido in pedidos if pedido.macropedido_id}
        _eventos_referencian_pedidos(ids).delete()
        Pedido.objects.filter(pk__in=ids).delete()
        for macro_id in sorted(macro_ids):
            macro = MacroPedido.objects.get(pk=macro_id)
            if macro.pedidos.exists():
                macro.recalcular_resumen()
            else:
                macro.delete()
    return resumen
