"""Aggregated, fail-closed POS retention-gap reconciliation (laboratory only).

No web endpoint advances a cursor. Private signing keys and archive bodies are
read only from POSIX 0600 files outside the release and are never logged.
"""

import base64
import io
import json
import os
import re
import uuid
import zipfile
from datetime import timedelta
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from django.conf import settings
from django.core.management.base import CommandError
from django.db import DatabaseError, connection, transaction
from django.db.models import Prefetch, Q
from django.utils import timezone

from .api_pos import (
    RetentionGap, _decode_cursor, _filter_fingerprint, _iso_utc,
    _latest_purge_epoch, _parse_aware_datetime, _purge_range_intersects,
    _serialize_pedido,
)
from .models import (
    ExportacionRetencion, ItemPedido, Pedido, PedidoPurgado,
    PosAggregatedCredential, PosRecoveryV2, PosRecoveryV2Freeze,
    PosRecoveryV2Nonce, PosRecoveryV2SigningKey, SucursalCliente,
)
from .pos_recovery import _decode_base64url, _private_file, _read_private, canonical, digest

MAX_ORDERS = 5000
MAX_ZIP = 100_000_000
PRESTATE_FIELDS = frozenset({
    "cursor", "ultimo_cursor_confirmado", "ventana_desde", "ventana_hasta",
    "agua_alta_hasta", "sucursales_origen", "estado", "version_api",
})
ACK_FIELDS = frozenset({
    "type", "key_id", "nonce", "issued_at", "recovery_id", "edge_id",
    "pos_branch_id", "sender_ids", "pos_prestate_sha256", "snapshot_sha256",
    "manifest_sha256", "orders_sha256", "tombstones_sha256", "received",
    "unresolved",
})
TRIGGERS = (
    "pedidos_recovery_v2_pedido_guard", "pedidos_recovery_v2_item_guard",
    "pedidos_recovery_v2_tombstone_guard",
)
DECIMAL_TEXT = re.compile(r"^-?[0-9]+(?:\.[0-9]+)?$")


def _strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="strict")
        return json.loads(data, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
    except (ValueError, UnicodeDecodeError, TypeError) as exc:
        raise CommandError("JSON inválido, ambiguo o no canónico.") from exc


def _b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _sender_ids(value):
    if (not isinstance(value, list) or not value or len(value) > 32
            or any(type(v) is not int or v <= 0 for v in value)
            or value != sorted(set(value))):
        raise CommandError("La tupla de remitentes debe ser exacta, ordenada y sin duplicados.")
    return value


def _uuid(value):
    try:
        parsed = uuid.UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise CommandError("UUID inválido.") from exc
    if str(parsed) != value:
        raise CommandError("UUID no canónico.")
    return parsed


def _identity(value, *, with_hash=False):
    expected = {"sender_id", "codigo_publico", "order_sha256"} if with_hash else {
        "sender_id", "codigo_publico"
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise CommandError("Identidad compuesta inválida.")
    sender = value["sender_id"]
    if type(sender) is not int or sender <= 0:
        raise CommandError("sender_id inválido.")
    public_id = str(_uuid(value["codigo_publico"]))
    if with_hash and (not isinstance(value["order_sha256"], str)
                      or len(value["order_sha256"]) != 64
                      or any(c not in "0123456789abcdef" for c in value["order_sha256"])):
        raise CommandError("Hash de pedido inválido.")
    return (sender, public_id)


def _prestate(raw, sender_ids, desde, hasta):
    state = _strict_json(raw)
    if not isinstance(state, dict) or set(state) != PRESTATE_FIELDS:
        raise CommandError("Preestado POS incompleto o con campos desconocidos.")
    if (_sender_ids(state["sucursales_origen"]) != sender_ids
            or state["estado"] != "reconciliacion" or state["version_api"] != "v2"):
        raise CommandError("Preestado POS no corresponde al alcance/estado esperado.")
    for field in ("cursor", "ultimo_cursor_confirmado"):
        if state[field] is not None and not isinstance(state[field], str):
            raise CommandError("Cursor del preestado inválido.")
        if isinstance(state[field], str) and len(state[field]) > 2048:
            raise CommandError("Cursor del preestado excede límite.")
        if isinstance(state[field], str):
            try:
                state[field].encode("ascii")
            except UnicodeEncodeError as exc:
                raise CommandError("Cursor del preestado no es ASCII.") from exc
    for field in ("ventana_desde", "ventana_hasta", "agua_alta_hasta"):
        if state[field] is not None:
            try:
                state[field] = _iso_utc(_parse_aware_datetime(state[field], field))
            except ValueError as exc:
                raise CommandError("Timestamp del preestado inválido.") from exc
    if state["ventana_desde"] != _iso_utc(desde) or state["ventana_hasta"] != _iso_utc(hasta):
        raise CommandError("Ventana del preestado no coincide.")
    if (state["agua_alta_hasta"] is not None
            and state["agua_alta_hasta"] != _iso_utc(desde)):
        raise CommandError("High-water no enlaza exactamente con la ventana.")
    return state


def _active_grant(edge_id, pos_branch_id, sender_ids):
    now = timezone.now()
    grants = PosAggregatedCredential.objects.filter(
        edge_id=edge_id, pos_branch_id=pos_branch_id,
        active=True, revoked_at__isnull=True,
    ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
    return any(grant.sender_ids == sender_ids and isinstance(grant.scopes, list)
               and "orders:v2:read" in grant.scopes for grant in grants)


def _approved_senders(sender_ids):
    return SucursalCliente.objects.filter(
        pk__in=sender_ids, tipo=SucursalCliente.Tipo.SUCURSAL, activa=True
    ).count() == len(sender_ids)


def _freeze(freeze_id, sender_ids, *, lock=False):
    if connection.vendor != "postgresql":
        raise CommandError("Recovery-v2 requiere PostgreSQL y barrera de escritura verificable.")
    query = PosRecoveryV2Freeze.objects
    if lock:
        query = query.select_for_update()
    freeze = query.filter(pk=freeze_id, active=True).first()
    if freeze is None or freeze.sender_ids != sender_ids:
        raise CommandError("Freeze inexistente, inactivo o de otro alcance.")
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT tgname, tgrelid::regclass::text, proname
            FROM pg_trigger JOIN pg_proc ON pg_proc.oid = tgfoid
            WHERE tgname = ANY(%s) AND tgenabled = 'O' AND NOT tgisinternal
        """, [list(TRIGGERS)])
        enabled = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
    if enabled != {
        TRIGGERS[0]: ("pedidos_pedido", "pedidos_recovery_v2_guard"),
        TRIGGERS[1]: ("pedidos_itempedido", "pedidos_recovery_v2_guard"),
        TRIGGERS[2]: ("pedidos_pedidopurgado", "pedidos_recovery_v2_guard"),
    }:
        raise CommandError("Barrera PostgreSQL incompleta o deshabilitada.")
    # Exercise the actual database guard, not merely the catalog entry. The
    # savepoint prevents a probe row from ever becoming durable.
    class ProbeEscaped(Exception):
        pass

    for sender_id in sender_ids:
        try:
            with transaction.atomic():
                Pedido.objects.create(
                    sucursal_cliente_id=sender_id, usuario_nombre="recovery-v2-probe",
                    estado=Pedido.Estado.CONFIRMADO, fecha_confirmacion=timezone.now(),
                )
                raise ProbeEscaped
        except DatabaseError as exc:
            cause = exc.__cause__
            if (getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)) != "55000":
                raise CommandError("La prueba de barrera falló por una causa distinta al freeze.") from exc
        except ProbeEscaped as exc:
            raise CommandError("Barrera inefectiva: la escritura de prueba fue admitida.") from exc
    return freeze


def start_freeze(*, freeze_id, sender_ids, reference):
    sender_ids = _sender_ids(sender_ids)
    if not 3 <= len(reference) <= 120:
        raise CommandError("Referencia de freeze inválida.")
    if not _approved_senders(sender_ids):
        raise CommandError("Remitente ausente, inactivo o no sucursal.")
    with transaction.atomic():
        # Drain any DML that entered the BEFORE trigger before the freeze row
        # became visible. The table locks stay held until the freeze commits;
        # later writers then see the committed row and fail in the trigger.
        with connection.cursor() as cursor:
            cursor.execute("""
                LOCK TABLE pedidos_pedido, pedidos_itempedido, pedidos_pedidopurgado
                IN SHARE ROW EXCLUSIVE MODE
            """)
        freeze = PosRecoveryV2Freeze.objects.create(
            freeze_id=freeze_id, sender_ids=sender_ids, reference=reference
        )
        _freeze(freeze_id, sender_ids, lock=True)
    return freeze


def _snapshot(sender_ids, desde, hasta):
    orders = list(Pedido.objects.filter(
        sucursal_cliente_id__in=sender_ids, estado=Pedido.Estado.CONFIRMADO,
        eliminado=False, fecha_confirmacion__gte=desde, fecha_confirmacion__lt=hasta,
    ).select_related("sucursal_cliente").prefetch_related(Prefetch(
        "items", queryset=ItemPedido.objects.select_related("producto").order_by("id")
    )).order_by("fecha_confirmacion", "id")[:MAX_ORDERS + 1])
    if len(orders) > MAX_ORDERS:
        raise CommandError("Baseline excede 5000 pedidos; intervención manual.")
    rows = [{"sender_id": p.sucursal_cliente_id, "order": _serialize_pedido(p, version=2)}
            for p in orders]
    rows.sort(key=lambda item: (item["sender_id"], item["order"]["codigo_publico"]))
    tombstones = list(PedidoPurgado.objects.filter(
        sucursal_cliente_id__in=sender_ids, fecha_confirmacion__isnull=False
    ).select_related("exportacion").order_by("sucursal_cliente_id", "codigo_publico")[:MAX_ORDERS + 1])
    if len(tombstones) > MAX_ORDERS:
        raise CommandError("Baseline excede 5000 tombstones; intervención manual.")
    erased = [{
        "sender_id": p.sucursal_cliente_id, "codigo_publico": str(p.codigo_publico),
        "pedido_id_origen": p.pedido_id_origen,
        "exportacion_id": str(p.exportacion_id) if p.exportacion_id else None,
        "archive_sha256": p.exportacion.sha256_archivo if p.exportacion_id else None,
    } for p in tombstones]
    erased.sort(key=lambda item: (item["sender_id"], item["codigo_publico"]))
    keys = [(r["sender_id"], r["order"]["codigo_publico"]) for r in rows]
    keys += [(r["sender_id"], r["codigo_publico"]) for r in erased]
    if len(keys) != len(set(keys)):
        raise CommandError("Identidad compuesta duplicada entre pedidos/tombstones.")
    return rows, erased


def _sign(payload, key, private_file):
    raw = _read_private(private_file, maximum=64)
    if len(raw) != 32:
        raise CommandError("Clave privada Ed25519 inválida.")
    private = Ed25519PrivateKey.from_private_bytes(raw)
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    if _b64(public) != key.public_key_b64 or not key.active or key.revoked_at:
        raise CommandError("Clave Pedidos no coincide con la pública activa registrada.")
    return {"payload": payload, "signature_b64": _b64(private.sign(canonical(payload)))}


def _verify(envelope, key):
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "signature_b64"}:
        raise CommandError("Envelope firmado inválido.")
    if not isinstance(envelope["payload"], dict):
        raise CommandError("Payload firmado inválido.")
    if not key.active or key.revoked_at:
        raise CommandError("Clave de firma revocada.")
    try:
        Ed25519PublicKey.from_public_bytes(_decode_base64url(key.public_key_b64, 32)).verify(
            _decode_base64url(envelope["signature_b64"], 64), canonical(envelope["payload"])
        )
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise CommandError("Firma inválida.") from exc
    return envelope["payload"]


def _write_private(path, content):
    path = _private_file(path, must_exist=False)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    if os.name == "posix":
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    return digest(content)


def prepare(*, recovery_id, edge_id, pos_branch_id, sender_ids, desde, hasta,
            prestate_file, freeze_id, pedidos_key_id, private_key_file, output,
            archive_dir, reference):
    sender_ids = _sender_ids(sender_ids)
    if not 3 <= len(reference) <= 120:
        raise CommandError("Referencia inválida.")
    try:
        desde = _parse_aware_datetime(desde, "desde")
        hasta = _parse_aware_datetime(hasta, "hasta")
    except ValueError as exc:
        raise CommandError("Ventana inválida.") from exc
    if hasta <= desde or hasta - desde > settings.POS_API_MAX_WINDOW:
        raise CommandError("Ventana fuera de límite.")
    state = _prestate(_read_private(prestate_file, maximum=8192), sender_ids, desde, hasta)
    prestate_sha = digest(canonical(state))
    existing = PosRecoveryV2.objects.filter(pk=recovery_id).first()
    if existing:
        candidate = _read_private(output, maximum=MAX_ZIP)
        if (existing.edge_id == edge_id and existing.pos_branch_id == pos_branch_id
                and existing.sender_ids == sender_ids and existing.desde == desde
                and existing.hasta == hasta and existing.pos_prestate_sha256 == prestate_sha
                and existing.freeze_id == freeze_id and existing.pedidos_key_id == pedidos_key_id
                and existing.snapshot_sha256 == digest(candidate)):
            return existing
        raise CommandError("Retry incompatible con el acta existente.")
    with transaction.atomic():
        freeze = _freeze(freeze_id, sender_ids, lock=True)
        if not _active_grant(edge_id, pos_branch_id, sender_ids) or not _approved_senders(sender_ids):
            raise CommandError("No hay grant vigente del Edge y tupla exacta.")
        key = PosRecoveryV2SigningKey.objects.filter(
            pk=pedidos_key_id, kind="pedidos", edge_id=edge_id,
            pos_branch_id=pos_branch_id, sender_ids=sender_ids, active=True,
            revoked_at__isnull=True,
        ).first()
        if key is None:
            raise CommandError("Clave Pedidos desconocida o de otro alcance.")
        epoch = _latest_purge_epoch(sender_ids)
        gap = _purge_range_intersects(desde, hasta, sender_ids)
        if state["cursor"]:
            try:
                _decode_cursor(state["cursor"], _filter_fingerprint(
                    desde, hasta, sender_ids, version=2
                ), desde, hasta, version=2, purge_epoch=epoch)
            except RetentionGap:
                gap = True
            except ValueError as exc:
                raise CommandError("Cursor ajeno al alcance o ventana.") from exc
        if not gap:
            raise CommandError("No se reprodujo retention_gap para este alcance.")
        rows, erased = _snapshot(sender_ids, desde, hasta)
        recovered = [proof for tombstone in erased
                     if (proof := _validated_archive(tombstone, archive_dir)) is not None]
        recovered.sort(key=lambda item: (item["sender_id"], item["order"]["codigo_publico"]))
        order_lines = b"".join(canonical(row) + b"\n" for row in rows)
        recovered_lines = b"".join(canonical(row) + b"\n" for row in recovered)
        tombstone_bytes = canonical(erased) + b"\n"
        manifest = {
            "type": "pedidos.recovery_manifest.v2", "key_id": str(key.pk),
            "recovery_id": str(recovery_id), "edge_id": str(edge_id),
            "pos_branch_id": str(pos_branch_id), "sender_ids": sender_ids,
            "pos_prestate": state, "pos_prestate_sha256": prestate_sha,
            "desde": _iso_utc(desde), "hasta": _iso_utc(hasta),
            "purge_epoch": epoch, "freeze_id": str(freeze.pk),
            "orders_sha256": digest(order_lines), "orders_count": len(rows),
            "recovered_orders_sha256": digest(recovered_lines),
            "recovered_orders_count": len(recovered),
            "tombstones_sha256": digest(tombstone_bytes), "tombstones_count": len(erased),
            "next_desde": _iso_utc(hasta), "next_cursor": None,
        }
        signed = _sign(manifest, key, private_key_file)
        manifest_bytes = canonical(signed) + b"\n"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in (
                ("manifest.json", manifest_bytes), ("orders.jsonl", order_lines),
                ("recovered_orders.jsonl", recovered_lines),
                ("tombstones.json", tombstone_bytes),
            ):
                info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                archive.writestr(info, content)
        content = buffer.getvalue()
        if (len(content) > MAX_ZIP
                or sum(map(len, (manifest_bytes, order_lines, recovered_lines, tombstone_bytes))) > MAX_ZIP
                or len(tombstone_bytes) > 2_000_000):
            raise CommandError("Snapshot excede límite; no hay cierre parcial.")
        if Path(output).exists() or Path(output).is_symlink():
            if _read_private(output, maximum=MAX_ZIP) != content:
                raise CommandError("Archivo huérfano incompatible; no se sobrescribe.")
            snapshot_hash = digest(content)
        else:
            snapshot_hash = _write_private(output, content)
        return PosRecoveryV2.objects.create(
            recovery_id=recovery_id, edge_id=edge_id, pos_branch_id=pos_branch_id,
            sender_ids=sender_ids, desde=desde, hasta=hasta,
            pos_prestate_sha256=prestate_sha, purge_epoch=epoch,
            snapshot_sha256=snapshot_hash, manifest_sha256=digest(manifest_bytes),
            orders_sha256=digest(order_lines), tombstones_sha256=digest(tombstone_bytes),
            recovered_orders_sha256=digest(recovered_lines),
            orders_count=len(rows), recovered_orders_count=len(recovered),
            tombstones_count=len(erased), freeze=freeze,
            pedidos_key=key, opened_reference=reference,
        )


def _zip_contents(raw):
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if (len(entries) != 4 or set(archive.namelist()) != {
                "manifest.json", "orders.jsonl", "recovered_orders.jsonl", "tombstones.json"
            } or sum(item.file_size for item in entries) > MAX_ZIP):
                raise CommandError("Snapshot incompleto, duplicado o sobredimensionado.")
            manifest = archive.read("manifest.json")
            lines = archive.read("orders.jsonl")
            recovered_lines = archive.read("recovered_orders.jsonl")
            tombstone_bytes = archive.read("tombstones.json")
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise CommandError("Snapshot ZIP dañado.") from exc
    return manifest, lines, recovered_lines, tombstone_bytes


def _validated_archive(tombstone, archive_dir):
    export_id = tombstone["exportacion_id"]
    if export_id is None:
        return None
    export = ExportacionRetencion.objects.filter(pk=export_id).first()
    if (export is None or export.estado != ExportacionRetencion.Estado.CONFIRMADA
            or export.sha256_archivo != tombstone["archive_sha256"]):
        return None
    path = Path(archive_dir) / f"{export_id}.zip"
    try:
        raw = _read_private(path, maximum=MAX_ZIP)
    except CommandError:
        return None
    if digest(raw) != export.sha256_archivo:
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if len(archive.infolist()) != 2 or set(archive.namelist()) != {
                "manifest.json", "pedidos.jsonl"
            } or sum(i.file_size for i in archive.infolist()) > MAX_ZIP:
                return None
            manifest = _strict_json(archive.read("manifest.json"))
            lines = archive.read("pedidos.jsonl")
    except (zipfile.BadZipFile, KeyError, CommandError):
        return None
    if (not isinstance(manifest, dict) or manifest.get("lote_id") != export_id
            or manifest.get("sha256_pedidos_jsonl") != digest(lines)
            or export.sha256_contenido != digest(lines)):
        return None
    matches = []
    for line in lines.splitlines():
        row = _strict_json(line)
        if (isinstance(row, dict) and row.get("codigo_publico") == tombstone["codigo_publico"]
                and isinstance(row.get("sucursal"), dict)
                and type(row["sucursal"].get("id")) is int
                and row["sucursal"]["id"] == tombstone["sender_id"]
                and type(row.get("id")) is int
                and row["id"] == tombstone["pedido_id_origen"]):
            converted = _archive_to_api_v2(row)
            if converted is not None:
                matches.append({
                    "sender_id": tombstone["sender_id"], "order": converted,
                    "archive_row_sha256": digest(canonical(row)),
                    "exportacion_id": export_id, "archive_sha256": export.sha256_archivo,
                })
    return matches[0] if len(matches) == 1 else None


def _archive_to_api_v2(row):
    """Convert a verified v1 retention row to the exact public v2 order shape."""
    try:
        if (row["estado"] != "confirmado"
                or row["eliminado"] is not False
                or not isinstance(row["items"], list)
                or type(row["id"]) is not int or row["id"] <= 0
                or not isinstance(row["sucursal"], dict)
                or set(row["sucursal"]) != {"id", "nombre", "tipo"}
                or type(row["sucursal"]["id"]) is not int
                or row["sucursal"]["tipo"] != "sucursal"
                or not isinstance(row["sucursal"]["nombre"], str)
                or not isinstance(row["total"], str)
                or not DECIMAL_TEXT.fullmatch(row["total"])):
            return None
        _uuid(row["codigo_publico"])
        confirmed = _iso_utc(_parse_aware_datetime(row["fecha_confirmacion"], "fecha_confirmacion"))
        items = []
        for item in row["items"]:
            if (not isinstance(item, dict)
                    or set(item) != {
                        "id", "producto_id", "producto_nombre", "producto_nombre_ticket",
                        "unidad_medida", "unidad_abreviatura", "cantidad_por_precio",
                        "cantidad", "precio_unitario", "subtotal",
                    }
                    or type(item["id"]) is not int or item["id"] <= 0
                    or type(item["producto_id"]) is not int or item["producto_id"] <= 0
                    or any(not isinstance(item[name], str) for name in (
                        "producto_nombre", "producto_nombre_ticket", "unidad_medida",
                        "unidad_abreviatura",
                    ))
                    or any(not isinstance(item[name], str) or not DECIMAL_TEXT.fullmatch(item[name])
                           for name in ("cantidad_por_precio", "cantidad", "precio_unitario", "subtotal"))):
                return None
            items.append({
                "id": item["id"], "pedido_id": row["id"],
                "producto": {
                    "id": item["producto_id"], "nombre": item["producto_nombre"],
                    "nombre_ticket": item["producto_nombre_ticket"],
                    "unidad_medida": item["unidad_medida"],
                    "unidad_abreviatura": item["unidad_abreviatura"],
                    "cantidad_por_precio": item["cantidad_por_precio"],
                },
                "cantidad": item["cantidad"],
                "precio_unitario": item["precio_unitario"],
                "subtotal": item["subtotal"],
            })
        return {
            "id": row["id"], "codigo_publico": row["codigo_publico"],
            "fecha_confirmacion": confirmed, "total": row["total"],
            "sucursal": row["sucursal"], "items": items,
        }
    except (KeyError, TypeError, ValueError, CommandError, AttributeError):
        return None


def _verified_ack(raw, record):
    envelope = _strict_json(raw)
    if not isinstance(envelope, dict) or not isinstance(envelope.get("payload"), dict):
        raise CommandError("ACK inválido.")
    ack = envelope["payload"]
    if set(ack) != ACK_FIELDS:
        raise CommandError("ACK con campos faltantes o inesperados.")
    key = PosRecoveryV2SigningKey.objects.filter(
        pk=_uuid(ack["key_id"]), kind="edge", edge_id=record.edge_id,
        pos_branch_id=record.pos_branch_id, sender_ids=record.sender_ids,
        active=True, revoked_at__isnull=True,
    ).first()
    if key is None:
        raise CommandError("Clave Edge ajena, revocada o de otro alcance.")
    _verify(envelope, key)
    nonce = _uuid(ack["nonce"])
    try:
        issued = _parse_aware_datetime(ack["issued_at"], "issued_at")
    except ValueError as exc:
        raise CommandError("Fecha ACK inválida.") from exc
    if (issued < record.opened_at - timedelta(minutes=5)
            or issued > timezone.now() + timedelta(minutes=5)
            or ack["type"] != "pedidos.edge.recovery_ack.v2"
            or ack["recovery_id"] != str(record.pk)
            or ack["edge_id"] != str(record.edge_id)
            or ack["pos_branch_id"] != str(record.pos_branch_id)
            or ack["sender_ids"] != record.sender_ids
            or ack["pos_prestate_sha256"] != record.pos_prestate_sha256
            or ack["snapshot_sha256"] != record.snapshot_sha256
            or ack["manifest_sha256"] != record.manifest_sha256
            or ack["orders_sha256"] != record.orders_sha256
            or ack["tombstones_sha256"] != record.tombstones_sha256):
        raise CommandError("ACK no corresponde exactamente a esta recuperación.")
    return ack, key, nonce


def complete(*, recovery_id, snapshot_file, snapshot_sha256, edge_ack_file,
             edge_ack_sha256, archive_dir, private_key_file, receipt_file, reference):
    if not 3 <= len(reference) <= 120:
        raise CommandError("Referencia inválida.")
    snapshot = _read_private(snapshot_file, maximum=MAX_ZIP)
    ack_raw = _read_private(edge_ack_file, maximum=10_000_000)
    if digest(snapshot) != snapshot_sha256 or digest(ack_raw) != edge_ack_sha256:
        raise CommandError("SHA-256 independiente del snapshot/ACK no coincide.")
    manifest_bytes, lines, recovered_lines, tombstone_bytes = _zip_contents(snapshot)
    signed_manifest = _strict_json(manifest_bytes)
    rows = [_strict_json(line) for line in lines.splitlines()]
    recovered_rows = [_strict_json(line) for line in recovered_lines.splitlines()]
    erased = _strict_json(tombstone_bytes)
    if (not isinstance(erased, list) or len(rows) > MAX_ORDERS
            or len(recovered_rows) > MAX_ORDERS or len(erased) > MAX_ORDERS):
        raise CommandError("Baseline incompleta o sobredimensionada.")
    receipt_path = Path(receipt_file)
    with transaction.atomic():
        record = PosRecoveryV2.objects.select_for_update().filter(pk=recovery_id).first()
        if record is None or record.snapshot_sha256 != snapshot_sha256:
            raise CommandError("Recuperación/snapshot no registrado.")
        if record.estado in ("completada", "intervencion_manual"):
            if (record.edge_ack_sha256 == edge_ack_sha256
                    and receipt_path.is_file()
                    and digest(_read_private(receipt_path, maximum=8192)) == record.receipt_sha256):
                return record
            if record.estado == "completada":
                raise CommandError("Cierre repetido con evidencia distinta.")
        _freeze(record.freeze_id, record.sender_ids, lock=True)
        if (not _active_grant(record.edge_id, record.pos_branch_id, record.sender_ids)
                or not _approved_senders(record.sender_ids)):
            raise CommandError("Grant Edge revocado o de otro alcance.")
        manifest = _verify(signed_manifest, record.pedidos_key)
        expected_manifest = {
            "type": "pedidos.recovery_manifest.v2", "key_id": str(record.pedidos_key_id),
            "recovery_id": str(record.pk), "edge_id": str(record.edge_id),
            "pos_branch_id": str(record.pos_branch_id), "sender_ids": record.sender_ids,
            "pos_prestate": manifest.get("pos_prestate"),
            "pos_prestate_sha256": record.pos_prestate_sha256,
            "desde": _iso_utc(record.desde), "hasta": _iso_utc(record.hasta),
            "purge_epoch": record.purge_epoch, "freeze_id": str(record.freeze_id),
            "orders_sha256": record.orders_sha256, "orders_count": record.orders_count,
            "recovered_orders_sha256": record.recovered_orders_sha256,
            "recovered_orders_count": record.recovered_orders_count,
            "tombstones_sha256": record.tombstones_sha256,
            "tombstones_count": record.tombstones_count,
            "next_desde": _iso_utc(record.hasta), "next_cursor": None,
        }
        if (manifest != expected_manifest
                or digest(canonical(manifest["pos_prestate"])) != record.pos_prestate_sha256
                or digest(manifest_bytes) != record.manifest_sha256
                or digest(lines) != record.orders_sha256 or len(rows) != record.orders_count
                or digest(recovered_lines) != record.recovered_orders_sha256
                or len(recovered_rows) != record.recovered_orders_count
                or digest(tombstone_bytes) != record.tombstones_sha256
                or len(erased) != record.tombstones_count):
            raise CommandError("Manifest/baseline no coincide con acta persistida.")
        current_rows, current_erased = _snapshot(record.sender_ids, record.desde, record.hasta)
        if (current_rows != rows or current_erased != erased
                or _latest_purge_epoch(record.sender_ids) != record.purge_epoch):
            raise CommandError("Drift bajo freeze; nuevo snapshot obligatorio.")
        expected = {}
        if rows != sorted(rows, key=lambda row: (row["sender_id"], row["order"]["codigo_publico"])):
            raise CommandError("Órdenes vigentes no están en orden canónico.")
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"sender_id", "order"}:
                raise CommandError("Fila de pedido inválida.")
            identity = _identity({
                "sender_id": row["sender_id"], "codigo_publico": row["order"]["codigo_publico"]
            })
            expected[identity] = digest(canonical(row["order"]))
        recovered_map = {}
        for recovered in recovered_rows:
            if not isinstance(recovered, dict) or set(recovered) != {
                "sender_id", "order", "archive_row_sha256", "exportacion_id", "archive_sha256"
            } or not isinstance(recovered["order"], dict):
                raise CommandError("Fila recuperada inválida.")
            identity = _identity({
                "sender_id": recovered["sender_id"],
                "codigo_publico": recovered["order"].get("codigo_publico"),
            })
            if identity in recovered_map or identity in expected:
                raise CommandError("Identidad compuesta duplicada en baseline.")
            recovered_map[identity] = recovered
        if recovered_rows != sorted(
            recovered_rows, key=lambda row: (row["sender_id"], row["order"]["codigo_publico"])
        ):
            raise CommandError("Órdenes recuperadas no están en orden canónico.")
        unproved = set()
        tombstone_ids = set()
        if erased != sorted(erased, key=lambda row: (row["sender_id"], row["codigo_publico"])):
            raise CommandError("Tombstones no están en orden canónico.")
        for tombstone in erased:
            if not isinstance(tombstone, dict) or set(tombstone) != {
                "sender_id", "codigo_publico", "pedido_id_origen",
                "exportacion_id", "archive_sha256"
            }:
                raise CommandError("Tombstone inválido.")
            identity = _identity({
                "sender_id": tombstone["sender_id"], "codigo_publico": tombstone["codigo_publico"]
            })
            tombstone_ids.add(identity)
            proof = _validated_archive(tombstone, archive_dir)
            if proof is None:
                unproved.add(identity)
            else:
                if recovered_map.get(identity) != proof:
                    raise CommandError("Prueba de archivo difiere de la baseline; nueva apertura requerida.")
                expected[identity] = digest(canonical(proof["order"]))
        if set(recovered_map) - tombstone_ids:
            raise CommandError("Baseline contiene orden recuperada sin tombstone.")
        ack, key, nonce = _verified_ack(ack_raw, record)
        received = ack["received"]
        unresolved = ack["unresolved"]
        if (not isinstance(received, list) or not isinstance(unresolved, list)
                or len(received) > 10000 or len(unresolved) > 10000):
            raise CommandError("Cobertura ACK inválida.")
        if (received != sorted(received, key=lambda item: _identity(item, with_hash=True))
                or unresolved != sorted(unresolved, key=_identity)):
            raise CommandError("ACK requiere identidades compuestas ordenadas.")
        received_map = {}
        for item in received:
            identity = _identity(item, with_hash=True)
            if identity in received_map:
                raise CommandError("ACK con identidad compuesta duplicada.")
            received_map[identity] = item["order_sha256"]
        unresolved_ids = [_identity(item) for item in unresolved]
        if len(unresolved_ids) != len(set(unresolved_ids)):
            raise CommandError("ACK con irresueltos duplicados.")
        if set(received_map) & set(unresolved_ids):
            raise CommandError("ACK contradictorio.")
        if PosRecoveryV2Nonce.objects.filter(key=key, nonce=nonce).exists():
            raise CommandError("Nonce Edge ya usado globalmente.")
        status = "completada" if (
            not unproved and not unresolved_ids and received_map == expected
        ) else "intervencion_manual"
        receipt_payload = {
            "type": "pedidos.recovery_receipt.v2", "key_id": str(record.pedidos_key_id),
            "recovery_id": str(record.pk), "edge_id": str(record.edge_id),
            "pos_branch_id": str(record.pos_branch_id), "sender_ids": record.sender_ids,
            "pos_prestate_sha256": record.pos_prestate_sha256,
            "snapshot_sha256": record.snapshot_sha256,
            "manifest_sha256": record.manifest_sha256,
            "edge_ack_sha256": edge_ack_sha256, "status": status,
            "next_desde": _iso_utc(record.hasta) if status == "completada" else None,
            "next_cursor": None, "issued_at": _iso_utc(timezone.now()),
        }
        if receipt_path.exists() or receipt_path.is_symlink():
            existing_receipt = _strict_json(_read_private(receipt_path, maximum=8192))
            existing_payload = _verify(existing_receipt, record.pedidos_key)
            if (set(existing_payload) != set(receipt_payload)
                    or any(existing_payload[name] != value for name, value in receipt_payload.items()
                           if name != "issued_at")):
                raise CommandError("Recibo huérfano incompatible; no se sobrescribe.")
            try:
                prior_issued = _parse_aware_datetime(existing_payload["issued_at"], "issued_at")
            except ValueError as exc:
                raise CommandError("Fecha del recibo huérfano inválida.") from exc
            if prior_issued < record.opened_at or prior_issued > timezone.now() + timedelta(minutes=5):
                raise CommandError("Fecha del recibo huérfano fuera de rango.")
            receipt_hash = digest(_read_private(receipt_path, maximum=8192))
        else:
            signed_receipt = _sign(receipt_payload, record.pedidos_key, private_key_file)
            receipt_hash = _write_private(receipt_path, canonical(signed_receipt) + b"\n")
        try:
            PosRecoveryV2Nonce.objects.create(key=key, nonce=nonce, recovery=record)
            record.estado = status
            record.edge_key = key
            record.edge_ack_sha256 = edge_ack_sha256
            record.edge_ack_nonce = nonce
            record.receipt_sha256 = receipt_hash
            record.closed_reference = reference
            record.closed_at = timezone.now() if status == "completada" else None
            record.save(update_fields=[
                "estado", "edge_key", "edge_ack_sha256", "edge_ack_nonce",
                "receipt_sha256", "closed_reference", "closed_at",
            ])
        except Exception:
            # Leave an fsynced, signed orphan for the identical retry. A
            # mismatching retry cannot reuse it, and no DB close was committed.
            raise
    return record
