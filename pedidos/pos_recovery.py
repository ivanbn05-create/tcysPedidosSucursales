"""Conciliación supervisada de una brecha POS v2.

El archivo temporal contiene pedidos y queda fuera del repositorio. El acta
duradera sólo guarda hashes/conteos. Ningún 410 se convierte automáticamente
en un checkpoint nuevo.
"""

import base64
import binascii
import hashlib
import io
import json
import os
import re
import stat
import uuid
import zipfile
from datetime import timedelta
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from django.conf import settings
from django.core.management.base import CommandError
from django.db import transaction
from django.db.models import Prefetch, Q
from django.utils import timezone

from .api_pos import (
    RetentionGap, _decode_cursor, _filter_fingerprint, _iso_utc,
    _latest_purge_epoch, _parse_aware_datetime, _purge_range_intersects,
    _serialize_pedido,
)
from .models import (
    ItemPedido, Pedido, PedidoPurgado, PosApiCredential, PosEdgeSigningKey, PosRetentionRecovery,
    SucursalCliente,
)

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
MAX_ORDERS = 5000
ACK_FIELDS = frozenset({
    "type", "key_id", "nonce", "issued_at", "recovery_id", "edge_id",
    "pos_branch_id", "sucursal_cliente_id", "snapshot_sha256", "orders_sha256",
    "old_cursor_sha256", "received_order_ids", "unresolved_order_ids",
})


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def operator_name():
    if os.name != "posix":
        return "unsupported"
    import pwd
    return pwd.getpwuid(os.geteuid()).pw_name


def _private_file(path, *, must_exist):
    path = Path(path)
    if os.name != "posix" or not path.is_absolute():
        raise CommandError("Se requiere una ruta absoluta en Linux/POSIX.")
    try:
        parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise CommandError("El directorio privado no existe.") from exc
    roots = [settings.BASE_DIR, settings.STATIC_ROOT, *settings.STATICFILES_DIRS]
    if any(parent.is_relative_to(Path(root).resolve()) for root in roots if root):
        raise CommandError("El artefacto debe quedar fuera del release y rutas públicas.")
    if any((part / ".git").exists() for part in (parent, *parent.parents)):
        raise CommandError("El artefacto debe quedar fuera de repositorios Git.")
    info = parent.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise CommandError("El directorio debe pertenecer al operador y tener modo 0700.")
    if must_exist:
        if path.is_symlink() or not path.is_file():
            raise CommandError("Falta el archivo privado o es un symlink.")
        info = path.stat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise CommandError("El archivo debe pertenecer al operador y tener modo 0600.")
    elif path.exists() or path.is_symlink():
        raise CommandError("El archivo de salida ya existe.")
    return path


def _read_private(path, *, maximum):
    path = _private_file(path, must_exist=True)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        with os.fdopen(fd, "rb") as stream:
            data = stream.read(maximum + 1)
    except Exception:
        raise
    if len(data) > maximum:
        raise CommandError("El archivo privado excede el límite permitido.")
    return data


def _json_bytes(data):
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CommandError("El acta privada no es JSON válido.") from exc
    if not isinstance(value, dict):
        raise CommandError("El acta privada debe ser un objeto JSON.")
    return value


def _snapshot(branch_id, desde, hasta):
    query = (
        Pedido.objects.filter(
            sucursal_cliente_id=branch_id,
            sucursal_cliente__tipo=SucursalCliente.Tipo.SUCURSAL,
            estado=Pedido.Estado.CONFIRMADO,
            eliminado=False,
            fecha_confirmacion__gte=desde,
            fecha_confirmacion__lt=hasta,
        )
        .select_for_update(of=("self",))
        .select_related("sucursal_cliente")
        .prefetch_related(Prefetch("items", queryset=ItemPedido.objects.select_related("producto").order_by("id")))
        .order_by("fecha_confirmacion", "id")
    )
    rows = []
    for order in query[: MAX_ORDERS + 1]:
        if len(rows) == MAX_ORDERS:
            raise CommandError("Más de 5000 pedidos: divide ventanas y solicita revisión manual.")
        rows.append(_serialize_pedido(order, version=2))
    # Un cursor puede volverse obsoleto por una purga FUERA de la ventana.
    # Exigir cobertura del ledger completo de esta sucursal impide que la
    # conciliación silencie pedidos faltantes de otra ventana.
    tombstones = sorted(str(value) for value in PedidoPurgado.objects.filter(
        sucursal_cliente_id=branch_id,
        fecha_confirmacion__isnull=False,
    ).values_list("codigo_publico", flat=True))
    if len(tombstones) > 5000:
        raise CommandError("Más de 5000 tombstones: requiere revisión manual.")
    return rows, tombstones


def _zip_snapshot(path, manifest, rows, tombstones):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as raw:
            with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", canonical(manifest) + b"\n")
                archive.writestr("orders.jsonl", b"".join(canonical(row) + b"\n" for row in rows))
                archive.writestr("tombstones.json", canonical(tombstones) + b"\n")
            raw.flush()
            os.fsync(raw.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        path.unlink(missing_ok=True)
        raise CommandError("No se pudo garantizar modo 0600.")
    if path.stat().st_size > 100_000_000:
        path.unlink(missing_ok=True)
        raise CommandError("Snapshot excede 100 MB; requiere revisión manual.")
    return digest(path.read_bytes())


def _read_zip_entry(archive, name, limit):
    with archive.open(name) as entry:
        data = entry.read(limit + 1)
    if len(data) > limit:
        raise CommandError("Snapshot descomprimido excede el límite permitido.")
    return data


def prepare(*, recovery_id, edge_id, pos_branch_id, branch_id, desde, hasta,
            old_cursor_file, output, reference):
    if not 3 <= len(reference) <= 120:
        raise CommandError("Referencia de apertura inválida.")
    output = Path(output)
    old_cursor_data = _read_private(old_cursor_file, maximum=2049)
    try:
        old_cursor = old_cursor_data.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise CommandError("Cursor previo inválido.") from exc
    try:
        desde = _parse_aware_datetime(desde, "desde")
        hasta = _parse_aware_datetime(hasta, "hasta")
    except ValueError as exc:
        raise CommandError("La ventana requiere timestamps ISO 8601 con zona.") from exc
    if hasta <= desde or hasta - desde > settings.POS_API_MAX_WINDOW:
        raise CommandError("Ventana inválida para la API v2.")
    old_cursor_hash = digest(old_cursor.encode("ascii"))
    existing = PosRetentionRecovery.objects.filter(pk=recovery_id).first()
    if existing:
        _private_file(output, must_exist=True)
        if (
            existing.edge_id == edge_id and existing.pos_branch_id == pos_branch_id
            and existing.sucursal_cliente_id == branch_id and existing.desde == desde
            and existing.hasta == hasta and existing.old_cursor_sha256 == old_cursor_hash
            and output.stat().st_size <= 100_000_000
            and digest(output.read_bytes()) == existing.snapshot_sha256
        ):
            return existing
        raise CommandError("ID de recuperación reutilizado con datos distintos o archivo ausente.")
    output = _private_file(output, must_exist=False)
    with transaction.atomic():
        credentials = PosApiCredential.objects.filter(
            edge_id=edge_id, pos_branch_id=pos_branch_id,
            sucursal_cliente_id=branch_id, active=True, revoked_at__isnull=True,
        ).filter(
            Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now())
        )
        if not any(
            isinstance(credential.scopes, list) and "orders:v2:read" in credential.scopes
            for credential in credentials
        ):
            raise CommandError("No existe una credencial activa con el mapeo Edge/POS/Pedidos aprobado.")
        branch = SucursalCliente.objects.filter(
            pk=branch_id, tipo=SucursalCliente.Tipo.SUCURSAL, activa=True
        ).first()
        if branch is None:
            raise CommandError("Sucursal de Pedidos no autorizada.")
        epoch = _latest_purge_epoch((branch_id,))
        gap = _purge_range_intersects(desde, hasta, (branch_id,))
        if old_cursor:
            try:
                _decode_cursor(old_cursor, _filter_fingerprint(desde, hasta, (branch_id,), version=2),
                               desde, hasta, version=2, purge_epoch=epoch)
            except RetentionGap:
                gap = True
            except ValueError as exc:
                raise CommandError("El cursor previo no corresponde a esta ventana y sucursal.") from exc
        if not gap:
            raise CommandError("No se reprodujo 410 retention_gap para este alcance.")
        rows, tombstones = _snapshot(branch_id, desde, hasta)
        order_lines = b"".join(canonical(row) + b"\n" for row in rows)
        tombstone_bytes = canonical(tombstones) + b"\n"
        if len(order_lines) > 95_000_000 or len(tombstone_bytes) > 2_000_000:
            raise CommandError("Contenido de baseline demasiado grande; requiere revisión manual.")
        manifest = {
            "version": "pedidos-recovery-v1", "recovery_id": str(recovery_id),
            "edge_id": str(edge_id), "pos_branch_id": str(pos_branch_id),
            "sucursal_cliente_id": branch_id, "desde": _iso_utc(desde), "hasta": _iso_utc(hasta),
            "old_cursor_sha256": old_cursor_hash, "purge_epoch": epoch,
            "orders_sha256": digest(order_lines), "orders_count": len(rows),
            "tombstones_sha256": digest(tombstone_bytes), "tombstones_count": len(tombstones),
            "next_desde": _iso_utc(hasta), "next_cursor": None,
        }
        try:
            file_hash = _zip_snapshot(output, manifest, rows, tombstones)
            record = PosRetentionRecovery.objects.create(
                recovery_id=recovery_id, edge_id=edge_id, pos_branch_id=pos_branch_id,
                sucursal_cliente=branch, desde=desde, hasta=hasta,
                old_cursor_sha256=old_cursor_hash, purge_epoch=epoch,
                snapshot_sha256=file_hash, orders_sha256=manifest["orders_sha256"],
                orders_count=len(rows), tombstones_sha256=manifest["tombstones_sha256"],
                tombstones_count=len(tombstones), referencia_apertura=reference,
                operador_apertura=operator_name(),
            )
        except Exception:
            output.unlink(missing_ok=True)
            raise
    return record


def _uuid_set(value):
    if not isinstance(value, list) or len(value) > MAX_ORDERS + 5000:
        raise CommandError("Lista de identidades inválida o demasiado grande.")
    try:
        normalized = [str(uuid.UUID(item)) for item in value]
    except (TypeError, ValueError, AttributeError) as exc:
        raise CommandError("Lista de identidades inválida.") from exc
    if len(normalized) != len(set(normalized)):
        raise CommandError("El acta contiene identidades duplicadas.")
    return set(normalized)


def _decode_base64url(value, length):
    if not isinstance(value, str) or not BASE64URL_RE.fullmatch(value):
        raise CommandError("Codificación del acuse Edge inválida.")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CommandError("Codificación del acuse Edge inválida.") from exc
    if len(raw) != length or base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != value:
        raise CommandError("Longitud o codificación del acuse Edge inválida.")
    return raw


def _verified_ack(envelope, record):
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "signature_b64"}:
        raise CommandError("Envelope del acuse Edge inválido.")
    ack = envelope["payload"]
    if not isinstance(ack, dict) or set(ack) != ACK_FIELDS:
        raise CommandError("Payload del acuse Edge inválido.")
    try:
        key_id = uuid.UUID(ack["key_id"])
        nonce = uuid.UUID(ack["nonce"])
        issued_at = _parse_aware_datetime(ack["issued_at"], "issued_at")
        bound_ids = ("recovery_id", "edge_id", "pos_branch_id")
        if any(str(uuid.UUID(ack[field])) != ack[field] for field in bound_ids):
            raise ValueError("UUID no canónico")
    except (TypeError, AttributeError, ValueError) as exc:
        raise CommandError("Identidad o fecha del acuse Edge inválida.") from exc
    if (type(ack["sucursal_cliente_id"]) is not int or ack["sucursal_cliente_id"] < 1
            or any(not isinstance(ack[field], str) or not SHA256_RE.fullmatch(ack[field])
                   for field in ("snapshot_sha256", "orders_sha256", "old_cursor_sha256"))
            or str(key_id) != ack["key_id"] or str(nonce) != ack["nonce"]
            or issued_at < record.creada_en - timedelta(minutes=5)
            or issued_at > timezone.now() + timedelta(minutes=5)):
        raise CommandError("Acuse Edge fuera de la recuperación o del tiempo permitido.")
    if ack["type"] != "pedidos.edge.recovery_ack.v1":
        raise CommandError("Tipo de acuse Edge no admitido.")
    key = PosEdgeSigningKey.objects.filter(
        pk=key_id, edge_id=record.edge_id, pos_branch_id=record.pos_branch_id,
        sucursal_cliente_id=record.sucursal_cliente_id,
        active=True, revoked_at__isnull=True,
    ).first()
    if key is None:
        raise CommandError("Clave de firma Edge desconocida, revocada o de otro alcance.")
    try:
        Ed25519PublicKey.from_public_bytes(_decode_base64url(key.public_key_b64, 32)).verify(
            _decode_base64url(envelope["signature_b64"], 64), canonical(ack)
        )
    except (InvalidSignature, ValueError) as exc:
        raise CommandError("Firma del acuse Edge inválida.") from exc
    if record.edge_ack_nonce == nonce and record.edge_ack_sha256:
        raise CommandError("Nonce del acuse Edge ya usado; emite un acuse nuevo.")
    return ack, key, nonce


def complete(*, recovery_id, snapshot_file, snapshot_sha256, edge_ack_file,
             edge_ack_sha256, custody_file, custody_sha256, freeze_evidence_sha256,
             reference):
    if not 3 <= len(reference) <= 120 or not all(SHA256_RE.fullmatch(item) for item in (
        snapshot_sha256, edge_ack_sha256, custody_sha256, freeze_evidence_sha256
    )):
        raise CommandError("Referencias o SHA-256 inválidos.")
    snapshot = _read_private(snapshot_file, maximum=100_000_000)
    ack_raw = _read_private(edge_ack_file, maximum=10_000_000)
    custody_raw = _read_private(custody_file, maximum=10_000_000)
    if digest(snapshot) != snapshot_sha256 or digest(ack_raw) != edge_ack_sha256 or digest(custody_raw) != custody_sha256:
        raise CommandError("Un archivo privado no coincide con su SHA-256 independiente.")
    ack_envelope = _json_bytes(ack_raw)
    custody = _json_bytes(custody_raw)
    try:
        with zipfile.ZipFile(io.BytesIO(snapshot)) as archive:
            if len(archive.infolist()) != 3 or set(archive.namelist()) != {
                "manifest.json", "orders.jsonl", "tombstones.json"
            }:
                raise CommandError("Snapshot con entradas inesperadas.")
            if any(entry.file_size > 100_000_000 for entry in archive.infolist()) or (
                sum(entry.file_size for entry in archive.infolist()) > 100_000_000
            ):
                raise CommandError("Snapshot descomprimido excede el límite permitido.")
            manifest = json.loads(_read_zip_entry(archive, "manifest.json", 1_000_000))
            lines = _read_zip_entry(archive, "orders.jsonl", 95_000_000)
            tombstone_bytes = _read_zip_entry(archive, "tombstones.json", 2_000_000)
            rows = [json.loads(line) for line in lines.splitlines()]
            tombstones = _uuid_set(json.loads(tombstone_bytes))
            if not isinstance(manifest, dict) or any(not isinstance(row, dict) for row in rows):
                raise CommandError("Snapshot con estructura inválida.")
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CommandError("Snapshot dañado o no válido.") from exc
    with transaction.atomic():
        record = PosRetentionRecovery.objects.select_for_update().filter(pk=recovery_id).first()
        if record is None or record.snapshot_sha256 != snapshot_sha256:
            raise CommandError("Recuperación o snapshot no registrado.")
        if record.estado == PosRetentionRecovery.Estado.COMPLETADA:
            if record.edge_ack_sha256 == edge_ack_sha256 and record.custody_sha256 == custody_sha256:
                return record
            raise CommandError("La recuperación ya se cerró con otra evidencia.")
        if (manifest.get("recovery_id") != str(recovery_id)
                or manifest.get("edge_id") != str(record.edge_id)
                or manifest.get("pos_branch_id") != str(record.pos_branch_id)
                or manifest.get("sucursal_cliente_id") != record.sucursal_cliente_id
                or manifest.get("old_cursor_sha256") != record.old_cursor_sha256
                or manifest.get("purge_epoch") != record.purge_epoch
                or manifest.get("desde") != _iso_utc(record.desde)
                or manifest.get("hasta") != _iso_utc(record.hasta)
                or digest(lines) != record.orders_sha256
                or len(rows) != record.orders_count
                or digest(tombstone_bytes) != record.tombstones_sha256
                or len(tombstones) != record.tombstones_count):
            raise CommandError("Manifest o contenido del snapshot no coincide con el acta.")
        order_ids = _uuid_set([row.get("codigo_publico") for row in rows])
        ack, ack_key, ack_nonce = _verified_ack(ack_envelope, record)
        if (ack.get("recovery_id") != str(recovery_id)
                or ack.get("edge_id") != str(record.edge_id)
                or ack.get("pos_branch_id") != str(record.pos_branch_id)
                or ack.get("sucursal_cliente_id") != record.sucursal_cliente_id
                or ack.get("snapshot_sha256") != record.snapshot_sha256
                or ack.get("orders_sha256") != record.orders_sha256
                or ack.get("old_cursor_sha256") != record.old_cursor_sha256):
            raise CommandError("Acuse Edge no corresponde a la recuperación.")
        received = _uuid_set(ack.get("received_order_ids"))
        unresolved = _uuid_set(ack.get("unresolved_order_ids"))
        archive_ids = _uuid_set(custody.get("restored_from_archive_ids"))
        prior_ids = _uuid_set(custody.get("already_on_edge_ids"))
        if custody.get("recovery_id") != str(recovery_id):
            raise CommandError("Acta de custodia no corresponde a la recuperación.")
        if (archive_ids | prior_ids) - tombstones:
            raise CommandError("Acta de custodia contiene UUID fuera de esta brecha.")
        credentials = PosApiCredential.objects.filter(
            edge_id=record.edge_id, pos_branch_id=record.pos_branch_id,
            sucursal_cliente_id=record.sucursal_cliente_id,
            active=True, revoked_at__isnull=True,
        ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))
        if not any(
            isinstance(credential.scopes, list) and "orders:v2:read" in credential.scopes
            for credential in credentials
        ):
            raise CommandError("No hay credencial v2 con scope vigente para reanudar este Edge.")
        # Si el conjunto cambió durante el procedimiento, no hay baseline
        # estable y jamás se emite un nuevo punto de continuidad.
        current_rows, current_tombstones = _snapshot(record.sucursal_cliente_id, record.desde, record.hasta)
        current_lines = b"".join(canonical(row) + b"\n" for row in current_rows)
        if (digest(current_lines) != record.orders_sha256 or current_tombstones != sorted(tombstones)
                or _latest_purge_epoch((record.sucursal_cliente_id,)) != record.purge_epoch):
            raise CommandError("Pedidos o purga cambiaron; requiere nueva baseline bajo freeze.")
        expected = order_ids | tombstones
        missing = expected - received
        unexpected = received - expected
        unsupported = tombstones - (archive_ids | prior_ids)
        if unresolved or missing or unexpected or unsupported:
            record.estado = PosRetentionRecovery.Estado.MANUAL
            record.referencia_cierre = reference
            record.operador_cierre = operator_name()
            record.edge_ack_sha256 = edge_ack_sha256
            record.edge_ack_key = ack_key
            record.edge_ack_nonce = ack_nonce
            record.custody_sha256 = custody_sha256
            record.freeze_evidence_sha256 = freeze_evidence_sha256
            record.save(update_fields=["estado", "referencia_cierre", "operador_cierre", "edge_ack_sha256",
                                       "edge_ack_key", "edge_ack_nonce",
                                       "custody_sha256", "freeze_evidence_sha256"])
            return record
        record.estado = PosRetentionRecovery.Estado.COMPLETADA
        record.referencia_cierre = reference
        record.operador_cierre = operator_name()
        record.edge_ack_sha256 = edge_ack_sha256
        record.edge_ack_key = ack_key
        record.edge_ack_nonce = ack_nonce
        record.custody_sha256 = custody_sha256
        record.freeze_evidence_sha256 = freeze_evidence_sha256
        record.cerrada_en = timezone.now()
        record.save(update_fields=["estado", "referencia_cierre", "operador_cierre", "edge_ack_sha256",
                                   "edge_ack_key", "edge_ack_nonce",
                                   "custody_sha256", "freeze_evidence_sha256", "cerrada_en"])
        return record
