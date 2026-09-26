"""Generate/verify public synthetic recovery-v2 E2E artifacts (never writes private keys).

Generation rotates ephemeral Ed25519 test keys; committed artifact bytes and
their SHA-256 values are the stable vector. Verification is reproducible.
Requires cryptography==50.0.1 and jsonschema==4.25.1 for verification.
"""

import base64
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "recovery-v2-e2e-fixture"
NAMES = ("archive.zip", "baseline.zip", "edge-ack.json", "receipt.json")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha(value):
    return hashlib.sha256(value).hexdigest()


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def raw64(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def signed(payload, private):
    return {"payload": payload, "signature_b64": b64(private.sign(canonical(payload)))}


def zipped(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, content)
    return output.getvalue()


def generate():
    seed = json.loads((ROOT / "recovery-v2-crypto-fixture.json").read_text(encoding="utf-8"))
    server = Ed25519PrivateKey.generate()
    edge = Ed25519PrivateKey.generate()
    current_lines = seed["orders_jsonl"].encode("utf-8")
    recovered = json.loads(seed["recovered_orders_jsonl"])
    order = recovered["order"]
    archive_row = {
        "id": order["id"], "codigo_publico": order["codigo_publico"],
        "sucursal": order["sucursal"], "fecha_confirmacion": order["fecha_confirmacion"],
        "total": order["total"], "estado": "confirmado", "eliminado": False,
        "items": [{
            "id": item["id"], "producto_id": item["producto"]["id"],
            "producto_nombre": item["producto"]["nombre"],
            "producto_nombre_ticket": item["producto"]["nombre_ticket"],
            "unidad_medida": item["producto"]["unidad_medida"],
            "unidad_abreviatura": item["producto"]["unidad_abreviatura"],
            "cantidad_por_precio": item["producto"]["cantidad_por_precio"],
            "cantidad": item["cantidad"], "precio_unitario": item["precio_unitario"],
            "subtotal": item["subtotal"],
        } for item in order["items"]],
    }
    archive_lines = canonical(archive_row) + b"\n"
    export_id = recovered["exportacion_id"]
    archive_manifest = {"lote_id": export_id, "sha256_pedidos_jsonl": sha(archive_lines)}
    archive_bytes = zipped((
        ("manifest.json", canonical(archive_manifest) + b"\n"),
        ("pedidos.jsonl", archive_lines),
    ))
    recovered["archive_sha256"] = sha(archive_bytes)
    recovered["archive_row_sha256"] = sha(canonical(archive_row))
    recovered_lines = canonical(recovered) + b"\n"
    tombstone = json.loads(seed["tombstones_json"])
    tombstone[0]["archive_sha256"] = sha(archive_bytes)
    tombstone_bytes = canonical(tombstone) + b"\n"
    manifest = dict(seed["manifest"]["payload"])
    manifest.update(
        orders_sha256=sha(current_lines), recovered_orders_sha256=sha(recovered_lines),
        tombstones_sha256=sha(tombstone_bytes),
    )
    manifest_bytes = canonical(signed(manifest, server)) + b"\n"
    baseline_bytes = zipped((
        ("manifest.json", manifest_bytes), ("orders.jsonl", current_lines),
        ("recovered_orders.jsonl", recovered_lines),
        ("tombstones.json", tombstone_bytes),
    ))
    ack = dict(seed["edge_ack"]["payload"])
    ack.update(
        snapshot_sha256=sha(baseline_bytes), manifest_sha256=sha(manifest_bytes),
        orders_sha256=sha(current_lines), tombstones_sha256=sha(tombstone_bytes),
    )
    ack_bytes = canonical(signed(ack, edge)) + b"\n"
    receipt = dict(seed["receipt"]["payload"])
    receipt.update(
        snapshot_sha256=sha(baseline_bytes), manifest_sha256=sha(manifest_bytes),
        edge_ack_sha256=sha(ack_bytes),
    )
    receipt_bytes = canonical(signed(receipt, server)) + b"\n"
    OUT.mkdir(exist_ok=True)
    contents = dict(zip(NAMES, (archive_bytes, baseline_bytes, ack_bytes, receipt_bytes)))
    for name, data in contents.items():
        (OUT / name).write_bytes(data)
    metadata = {
        "test_only": True,
        "note": "Public synthetic E2E vector; no private key or production data is included.",
        "pedidos_public_key_b64": b64(server.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)),
        "edge_public_key_b64": b64(edge.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)),
        "sha256": {name: sha(data) for name, data in contents.items()},
        "manifest_sha256": sha(manifest_bytes),
        "archive_row_sha256": sha(canonical(archive_row)),
        "negative_cases": [
            "signature_mutation", "wrong_edge_or_branch", "sender_tuple_changed",
            "nonce_reused", "archive_zip_changed", "archive_row_changed",
            "missing_recovered_order", "historical_terminal_order",
            "nonempty_cursor_midwindow", "inflight_writer_before_freeze",
            "receipt_missing", "checkpoint_without_completed_receipt",
        ],
    }
    (OUT / "metadata.json").write_bytes(canonical(metadata) + b"\n")
    verify()


def verify():
    metadata = json.loads((OUT / "metadata.json").read_bytes())
    schema = json.loads((ROOT / "recovery-v2.schema.json").read_bytes())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    for bad in ({}, False, {"payload": {}, "signature_b64": "a" * 86}):
        assert not validator.is_valid(bad), "schema accepted an invalid root"
    contents = {name: (OUT / name).read_bytes() for name in NAMES}
    for name, raw in contents.items():
        assert sha(raw) == metadata["sha256"][name], name
    with zipfile.ZipFile(io.BytesIO(contents["archive.zip"])) as archive:
        assert len(archive.infolist()) == 2
        assert set(archive.namelist()) == {"manifest.json", "pedidos.jsonl"}
        archive_manifest = json.loads(archive.read("manifest.json"))
        archive_lines = archive.read("pedidos.jsonl")
        assert archive_manifest["sha256_pedidos_jsonl"] == sha(archive_lines)
        archive_row = json.loads(archive_lines)
        assert sha(canonical(archive_row)) == metadata["archive_row_sha256"]
    with zipfile.ZipFile(io.BytesIO(contents["baseline.zip"])) as baseline:
        assert len(baseline.infolist()) == 4
        assert set(baseline.namelist()) == {
            "manifest.json", "orders.jsonl", "recovered_orders.jsonl", "tombstones.json"
        }
        manifest_bytes = baseline.read("manifest.json")
        manifest = json.loads(manifest_bytes)
        orders = baseline.read("orders.jsonl")
        recovered = baseline.read("recovered_orders.jsonl")
        tombstones = baseline.read("tombstones.json")
    ack = json.loads(contents["edge-ack.json"])
    receipt = json.loads(contents["receipt.json"])
    for envelope in (manifest, ack, receipt):
        validator.validate(envelope)
    for bad in (
        {"payload": {**receipt["payload"], "next_desde": None},
         "signature_b64": receipt["signature_b64"]},
        {"payload": {**receipt["payload"], "status": "intervencion_manual"},
         "signature_b64": receipt["signature_b64"]},
    ):
        assert not validator.is_valid(bad), "schema accepted an impossible receipt"
    for envelope, public in (
        (manifest, metadata["pedidos_public_key_b64"]),
        (ack, metadata["edge_public_key_b64"]),
        (receipt, metadata["pedidos_public_key_b64"]),
    ):
        public_key = Ed25519PublicKey.from_public_bytes(raw64(public))
        signature = raw64(envelope["signature_b64"])
        public_key.verify(signature, canonical(envelope["payload"]))
        changed = {**envelope["payload"], "sender_ids": [1]}
        try:
            public_key.verify(signature, canonical(changed))
        except InvalidSignature:
            pass
        else:
            raise AssertionError("mutated signed scope was accepted")
    payload = manifest["payload"]
    assert sha(manifest_bytes) == metadata["manifest_sha256"]
    assert payload["pos_prestate_sha256"] == sha(canonical(payload["pos_prestate"]))
    assert payload["pos_prestate"]["sucursales_origen"] == payload["sender_ids"]
    assert payload["pos_prestate"]["ventana_desde"] == payload["desde"]
    assert payload["pos_prestate"]["ventana_hasta"] == payload["hasta"]
    assert payload["orders_sha256"] == sha(orders)
    assert payload["recovered_orders_sha256"] == sha(recovered)
    assert payload["tombstones_sha256"] == sha(tombstones)
    assert payload["orders_count"] == len(orders.splitlines())
    assert payload["recovered_orders_count"] == len(recovered.splitlines())
    assert payload["tombstones_count"] == len(json.loads(tombstones))
    recovered_row = json.loads(recovered)
    current_row = json.loads(orders)
    tombstone = json.loads(tombstones)[0]
    assert archive_manifest["lote_id"] == tombstone["exportacion_id"]
    assert recovered_row["exportacion_id"] == tombstone["exportacion_id"]
    assert recovered_row["sender_id"] == tombstone["sender_id"]
    assert recovered_row["order"]["codigo_publico"] == tombstone["codigo_publico"]
    assert archive_row["id"] == tombstone["pedido_id_origen"]
    assert recovered_row["archive_sha256"] == sha(contents["archive.zip"])
    assert recovered_row["archive_row_sha256"] == sha(canonical(archive_row))
    assert recovered_row["sender_id"] == archive_row["sucursal"]["id"]
    assert recovered_row["order"]["codigo_publico"] == archive_row["codigo_publico"]
    assert recovered_row["order"]["id"] == archive_row["id"]
    assert recovered_row["order"]["sucursal"] == archive_row["sucursal"]
    assert recovered_row["order"]["total"] == archive_row["total"]
    assert recovered_row["order"]["fecha_confirmacion"] == archive_row["fecha_confirmacion"]
    assert len(recovered_row["order"]["items"]) == len(archive_row["items"])
    for item, original in zip(recovered_row["order"]["items"], archive_row["items"]):
        assert item["id"] == original["id"]
        assert item["pedido_id"] == archive_row["id"]
        assert item["producto"]["id"] == original["producto_id"]
        assert item["cantidad"] == original["cantidad"]
        assert item["precio_unitario"] == original["precio_unitario"]
        assert item["subtotal"] == original["subtotal"]
    assert ack["payload"]["snapshot_sha256"] == sha(contents["baseline.zip"])
    assert ack["payload"]["manifest_sha256"] == sha(manifest_bytes)
    for field in ("recovery_id", "edge_id", "pos_branch_id", "sender_ids", "pos_prestate_sha256",
                  "orders_sha256", "tombstones_sha256"):
        assert ack["payload"][field] == payload[field]
    expected_received = {
        (row["sender_id"], row["order"]["codigo_publico"]): sha(canonical(row["order"]))
        for row in (current_row, recovered_row)
    }
    actual_received = {
        (row["sender_id"], row["codigo_publico"]): row["order_sha256"]
        for row in ack["payload"]["received"]
    }
    assert len(ack["payload"]["received"]) == len(actual_received)
    assert actual_received == expected_received
    assert not ack["payload"]["unresolved"]
    wrong_received = dict(actual_received)
    wrong_received[next(iter(wrong_received))] = "0" * 64
    assert wrong_received != expected_received
    assert receipt["payload"]["edge_ack_sha256"] == sha(contents["edge-ack.json"])
    assert receipt["payload"]["snapshot_sha256"] == sha(contents["baseline.zip"])
    for field in ("recovery_id", "edge_id", "pos_branch_id", "sender_ids", "pos_prestate_sha256"):
        assert receipt["payload"][field] == payload[field]
    assert receipt["payload"]["manifest_sha256"] == sha(manifest_bytes)
    assert receipt["payload"]["status"] == "completada"
    assert receipt["payload"]["next_desde"] == payload["hasta"]
    print("recovery-v2 E2E public fixture: schema, signatures and hashes OK")


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--generate":
        generate()
    elif len(sys.argv) == 1:
        verify()
    else:
        raise SystemExit("usage: python recovery_v2_fixture_tool.py [--generate]")
