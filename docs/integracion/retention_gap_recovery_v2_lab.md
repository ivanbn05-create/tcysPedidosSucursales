# Recovery-v2 agregado — contrato candidato de laboratorio

No sustituye `recovery-v1`, API v1, Supabase legacy ni la purga. No está
autorizado para producción. El contrato sólo se congela cuando integrador y
POS ejecuten el mismo fixture/E2E y confirmen los SHA publicados.

## Alcance e identidad

Una credencial `PosAggregatedCredential` autentica **un** Edge UUID, **una**
POS Branch UUID y una tupla ordenada exacta de `SucursalCliente.id` activos
de tipo `sucursal`, con scope `orders:v2:read`. Para el ensayo de Arboledas la
tupla aprobada sólo en laboratorio es `[1,2,3,7,8,9]` y la POS Branch UUID es
`93a42904-4d09-4a50-94cf-2edbf5aa0e51`. El Edge UUID se obtiene del
registro técnico, no se infiere por nombre. El bearer antiguo singular y v1
permanecen separados. La rotación de bearer no altera el cursor ni los IDs de
pedido. La revocación auditada usa `revoke_pos_v2_aggregate_credential
--credential-id ... --referencia ... --confirm`; un bearer revocado no abre ni
cierra recovery.

Todas las identidades de pedido y tombstone son la **pareja**
`(sender_id,codigo_publico)`. No se acepta un UUID sin remitente. El Edge
solicita la tupla completa en `sucursal_id=1,2,3,7,8,9`; un subconjunto
puede servir a la API de lectura, pero **no** abre/cierra este recovery ni
autoriza checkpoint parcial.

## Protocolo de archivos y firmas

Canonical JSON = UTF-8 de `json.dumps(value, ensure_ascii=False,
sort_keys=True, separators=(",", ":"))`, sin NaN/Infinity ni claves duplicadas.
Ed25519 firma los bytes canónicos **del payload**, no del envelope. Las firmas
y claves públicas usan base64url sin padding. La clave privada Pedidos se lee
de archivo POSIX `0600` fuera de release/Git; la privada Edge permanece en
Edge. Se registran `key_id`, clave pública, tipo (`pedidos`/`edge`), Edge,
POS Branch y tupla. Una clave revocada no firma ni valida; la rotación exige
pin nuevo aprobado antes de abrir otra baseline. El POS pinnea la pública
Pedidos por `key_id`; no acepta una clave anunciada por el mismo ZIP como
única autoridad. Sólo claves **públicas** sintéticas y firmas de vector se
versionan en `schemas/recovery-v2-crypto-fixture.json`.
El pin público POS se guarda con `key_id`, versión de formato, vigencia y
revocación en `certs/` de la instalación, incluido en backup/restore H18; un
restore sin pin confiable no acepta recibos. Nunca se entrega la privada
Pedidos al Edge.
El Edge genera su par Ed25519 fuera de Git/logs/ZIP; su privada queda bajo ACL
de servicio en volumen privado persistente incluido en H18/BitLocker (no
DPAPI ligado a una VM concreta). Pedidos sólo recibe la pública con
`registrar_clave_agregada_pos_v2`. La rotación registra un nuevo `key_id`
antes de revocar el anterior; una clave anterior **activa** puede terminar un
acta ya abierta, una **revocada** no puede firmar ACK nuevos ni cerrar. La
revocación auditable usa `revocar_clave_agregada_pos_v2 --key-id ...
--referencia ... --confirm`. Un bearer POS nunca equivale a una firma.

`manifest_sha256` es SHA-256 de los bytes exactos de la entrada ZIP
`manifest.json`: `canonical({payload,signature_b64}) + b"\n"`.
`snapshot_sha256` es SHA-256 de **todos los bytes del ZIP final**, y por eso
no aparece dentro del manifest (evita autorreferencia). Los SHA de
`orders.jsonl`, `recovered_orders.jsonl` y `tombstones.json` incluyen el salto
de línea final. El
`edge_ack_sha256` del recibo es SHA-256 de los bytes exactos del archivo ACK
privado, no de una reserialización; el POS coteja ese archivo por hash.

El `prestate.json` contiene **todos** los campos de
`schemas/recovery-v2.schema.json#/$defs/prestate`, incluyendo el `cursor`
real (que puede estar vacío o no), `ultimo_cursor_confirmado`, ventana,
high-water, remitentes, estado
`reconciliacion` y versión `v2`. Pedidos lo normaliza a UTC y firma el objeto
completo dentro de `manifest.json`, además de su SHA-256. El ZIP privado
`0600` contiene exactamente `manifest.json`, `orders.jsonl`,
`recovered_orders.jsonl` y `tombstones.json`. `manifest.json` es un envelope
Ed25519 Pedidos; `orders`
tiene `{sender_id,order}` por línea con el payload de API v2, ordenado por
`(sender_id,codigo_publico)`; los tombstones también siguen ese orden y
tienen `sender_id`, `codigo_publico`, `pedido_id_origen`, `exportacion_id` y
SHA-256 del archivo custodio. `recovered_orders.jsonl` contiene una línea
por tombstone con archivo probado: `{sender_id,order,archive_row_sha256,
exportacion_id,archive_sha256}`. `order` es la conversión determinista al
objeto API v2, ordenada por pareja; el manifest firma su SHA y conteo. El
límite es 5 000 pedidos, 5 000 tombstones y 100 MB comprimidos/
descomprimidos. Excederlo bloquea; no se trunca ni pagina con checkpoint
parcial.

El Edge verifica firma/clave pinneada, SHA del ZIP recibido por canal
independiente, hashes de entradas, tupla exacta y preestado **bajo transacción
local** antes de importar. Guarda los pedidos de forma idempotente por pareja
sender/UUID, manteniendo el cursor anterior y `reconciliacion` ante crash. Su
ACK Ed25519 enumera `received` con pareja y SHA-256 del pedido importado;
`unresolved` enumera parejas sin contenido. Usa nonce UUID nuevo, global por
`key_id` en el ledger Pedidos, aun entre recoveries. Un nonce ya usado se
rechaza. El ACK debe contener todos los hashes y el preestado indicado en el
schema; no basta un listado de UUID autoafirmados.
Para pedidos vigentes **y** recuperados, `received.order_sha256` es siempre
SHA-256 de `canonical(order)` del objeto API v2, sin wrapper `sender_id`.
`archive_row_sha256` prueba la fila original del ZIP de exportación y es un
hash distinto. Si la conversión histórica no produce el esquema API v2 o el
importador POS rechaza precio/mesa, Edge marca `unresolved`, nunca un UUID
vacío. Un pedido archivado en estado `enviado` o `recibido` tampoco se
convierte en tarea nueva: exige intervención manual para reconciliar su
estado operativo. El cursor real, incluso no vacío a mitad de ventana,
permanece en el preestado firmado hasta recibir el cierre válido.
Ambos arrays del ACK se ordenan estrictamente por pareja y no admiten
duplicados ni solapamientos. Un pedido preexistente en POS sin cuerpo
persistido del que recalcular el hash canónico **no** cuenta como recibido:
queda en `unresolved`/manual. El POS persiste nonce/ACK antes de enviarlo para
que un timeout o reinicio no genere un ACK distinto inadvertidamente.

Para **cada** tombstone, Pedidos exige el ZIP de exportación confirmado bajo
`<archive-dir>/<exportacion_id>.zip`, 0600, fuera de Git/release. Coteja SHA
del ZIP con `ExportacionRetencion.sha256_archivo`, SHA de `pedidos.jsonl`,
manifest de exportación y fila de la pareja sender/UUID/ID original; el hash
canónico de esa fila debe coincidir con `archive_row_sha256` de la cuarta
entrada firmada. Pedidos revalida la conversión API v2 y el hash de ACK Edge.
Tombstone sin archivo verificable,
incluidas purgas por antigüedad sin exportación, queda en
`intervencion_manual` aunque Edge declare tenerlo. No se consulta el nombre
como prueba y no se recupera contenido purgado desde el tombstone.
El ZIP custodio se entrega **también al Edge** por canal privado: el Edge
comprueba el SHA del manifest firmado y de la fila original, e importa el
objeto API v2 de `recovered_orders.jsonl` antes de firmar `received`; una
mera afirmación del servidor no basta.

Con freeze todavía activo, el cierre relee pedidos/tombstones y época de
purga. Un cambio invalida la baseline y exige abrir otra. Si falta o sobra
cualquier pareja, hay irresueltos, una firma/scope/nonce inválido o una prueba
de archivo insuficiente, **ningún** checkpoint se autoriza. Pedidos emite un
recibo Ed25519 con estado `completada` o `intervencion_manual`; sólo el primero
incluye `next_desde=hasta`. El recibo liga recuperación, Edge, POS Branch,
tupla, SHA de preestado/manifest/ZIP/ACK y key_id. El POS verifica clave
pinneada y estos vínculos y, sólo entonces, en una transacción SQLite cambia
`ventana_desde` y `agua_alta_hasta` al `hasta` cubierto, cursor y
`ultimo_cursor_confirmado` a vacío, y `ventana_hasta` a `null` hasta que el
siguiente poll calcule una ventana contigua bajo el límite normal de la API;
marca `listo` en la misma transacción. Un preestado con high-water superior
al `hasta` cubierto se rechaza. Si `agua_alta_hasta` no es `null`, debe ser
**igual** a `ventana_desde`; un estado legado anómalo no se normaliza durante
recovery. Primera ventana con agua alta `null` conserva explícita la ausencia
de continuidad previa. Nunca se retrocede ni salta tiempo.
Crash entre importación
y recibo mantiene `reconciliacion`; retry de cierre idéntico devuelve el mismo
acta/recibo. ACK perdido se reenvía idéntico para el mismo cierre; para una
revisión manual posterior se firma ACK nuevo con nonce nuevo. Una firma
inválida no consume nonce.

## Freeze comprobable

La migración 0020 instala triggers PostgreSQL para INSERT/UPDATE/DELETE de
`Pedido`, `ItemPedido` y `PedidoPurgado` en los remitentes congelados. El
freeze persiste tupla, inicio, fin y referencias. Al iniciarlo se bloquean
las tres tablas hasta confirmar la fila activa, drenando transacciones de
escritura previas; ningún snapshot se firma antes de ese commit.
Apertura/cierre verifican que los tres triggers estén habilitados y ligados
a las tablas y función esperadas, y hacen una **escritura de prueba revertida
por cada remitente**; sólo el rechazo SQLSTATE `55000` demuestra la barrera. Si la
prueba falla, no se emite snapshot/recibo. La transacción de cierre conserva
la barrera hasta registrar nonce y acta; liberarla requiere comando separado.
La verificación también relee hashes y época para detectar cambios en
catálogos relacionados. Estos triggers son una barrera operativa, no un
mecanismo contra superusuarios que deshabiliten triggers. El corte exige
además detener escritores/colas y documentar el operador.

## Secuencia de laboratorio

Ejecutar como operador OS autorizado, con directorio privado `0700`, archivos
`0600`, PostgreSQL aislado y claves privadas fuera del repositorio. No usar
la base productiva ni copiar su `.env` a un release.

1. Provisionar bearer agregado con `issue_pos_v2_aggregate_credential`
   (`--edge-id`, `--pos-branch-id`, `--sender-ids 1,2,3,7,8,9`, `--output`,
   `--reference`, `--confirm`). Registrar las públicas Ed25519 con
   `registrar_clave_agregada_pos_v2` (`--kind edge|pedidos`, `--key-id`,
   mismos IDs, `--public-key-file`, `--referencia`, `--confirm`).
2. Congelar con `iniciar_freeze_pos_v2 --freeze-id UUID --sender-ids
   1,2,3,7,8,9 --referencia TICKET --confirm`; comprobar que una escritura
   sintética dirigida al remitente se rechaza. Reproducir `410 retention_gap`
   con exactamente la misma tupla/ventana/cursor del POS.
3. Ejecutar `preparar_recuperacion_agregada_pos_v2` con `--recovery-id`,
   `--edge-id`, `--pos-branch-id`, `--sender-ids`, `--desde`, `--hasta`,
   `--prestate-file`, `--freeze-id`, `--pedidos-key-id`, `--private-key-file`,
   `--archive-dir`,
   `--archivo`, `--referencia`, `--confirm`. Entregar ZIP y hash por canales
   independientes.
4. Tras importación durable en Edge, ejecutar
   `cerrar_recuperacion_agregada_pos_v2` con `--recovery-id`,
   `--snapshot-file`, `--snapshot-sha256`, `--edge-ack-file`,
   `--edge-ack-sha256`, `--archive-dir`, `--private-key-file`,
   `--receipt-file`, `--referencia`, `--confirm`. Entregar recibo firmado.
5. Tras verificar checkpoint POS o documentar intervención manual, liberar
   con `finalizar_freeze_pos_v2 --freeze-id UUID --referencia TICKET
   --confirm`; si quedaron actas pendientes, exige además `--abort-pending`
   y las marca manuales sin avanzar cursor.

| HTTP / evento | Acción Edge |
| --- | --- |
| `200` | Importar página y cursor en una transacción normal; deduplicar por pareja sender/UUID. |
| `401`/`403` | Detener; corregir bearer o alcance sin alterar checkpoint. |
| `410 retention_gap` | Fijar `reconciliacion`, persistir preestado completo, detener cursor, abrir procedimiento deliberado. |
| ACK enviado sin recibo | Mantener `reconciliacion`; reintentar mismo ACK, no crear nonce nuevo por timeout. |
| Recibo `intervencion_manual`/firma inválida | No avanzar; ticket y custodia. |
| Recibo `completada` válido | Una transacción local verifica preestado y cambia checkpoint de toda la tupla. |
| Timeout/`429`/`5xx` | Reintentar con backoff conservando cursor, pedidos y tupla. |

El vector criptográfico versionado tiene SHA de ZIP/archivo **marcadores**;
no es una autorización de cierre. Las pruebas PostgreSQL generan ZIP y
archivos reales efímeros, y los negativos de precio histórico/mesa POS se
resuelven en `unresolved`/manual; Pedidos jamás fuerza al POS a omitir esas
validaciones.

Para el parser/E2E Edge se versiona además el paquete público sintético
`schemas/recovery-v2-e2e-fixture/`: ZIP custodio, ZIP baseline de cuatro
entradas, ACK y recibo firmados, claves **públicas** y SHA exactos en
`metadata.json`. No contiene privadas ni datos productivos. Verificarlo con
Python 3.13.12, `cryptography==50.0.1` y `jsonschema==4.25.1` mediante
`python docs/integracion/schemas/recovery_v2_fixture_tool.py`. El validador
usa Draft 2020-12 y FormatChecker, rechaza raíces vacías, recibos con
`next_desde` incompatible, prueba firmas/ZIP/cadena de exportación y coteja
cobertura/hash de cada pedido ACK. `--generate` rota claves efímeras de
prueba y reemplaza el vector; no se usa después de congelar sus SHA. El
vector criptográfico anterior sigue siendo sólo una prueba de canonical JSON
y Ed25519, no un ZIP ejecutable.

Los negativos `nonce_reused`, `inflight_writer_before_freeze`, terminal
histórico, cursor no vacío, firma/alcance incorrectos y ausencia de archivo
están cubiertos por `pedidos/test_pos_recovery_v2.py` en PostgreSQL/POSIX;
el POS debe ejecutar además su propio CAS, precio/mesa y restore. Un backup
del acta DB **sin** los ZIP/ACK/recibos privados no basta: ante pérdida del
recibo el replay es fail-closed y no autoriza checkpoint. La custodia y
restauración coherente de estos archivos con sus SHA es gate explícito del
E2E/corte; no se considera resuelto por este fixture público.

Negativos obligatorios del E2E conjunto: firma Pedidos/Edge incorrecta,
`key_id` revocado, nonce repetido en otra acta, Edge/Branch/remitente ajeno,
orden repetida con hash diferente, orden y tombstone con la misma pareja,
archivo de exportación ausente/alterado, preestado/high-water incompatible,
escritura durante freeze, recibo perdido/reintentado, poll concurrente cuyo
CAS falla, precio histórico distinto del precio vigente y Mesa POS ocupada.
Los dos últimos deben quedar `unresolved`/manual hasta que haya una prueba
histórica y una capacidad de importación aprobadas; no se reescribe el precio
ni se despeja una Mesa para hacer pasar el test.
