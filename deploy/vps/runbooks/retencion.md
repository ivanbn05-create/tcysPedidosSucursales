# Exportación y retención de Pedidos en el VPS

Estado: código y procedimiento para ensayo; **purga real deshabilitada**.
`RETENTION_PURGE_ENABLED`, `RETENTION_EXPORT_CLEANUP_ENABLED`,
`RETENTION_BACKFILL_ENABLED` y
`RETENTION_RESTORE_ISOLATED` son `False` por defecto. No se instala timer ni se
ejecuta nada al iniciar Gunicorn. La clasificación está en
[`docs/retencion/matriz_datos.md`](../../../docs/retencion/matriz_datos.md).

La regla de elegibilidad para `Pedido` y sus `ItemPedido` es el primero de:
confirmación de archivo íntegro en equipo autorizado o 30 días desde
`first_received_at`. El borrado es físico. `MacroPedido` se elimina sólo cuando
no quedan hijos; sucursales, catálogo, precios, configuración y usuarios no se
purgan. Eventos de auditoría con `pedido_id` conocido se eliminan con el pedido;
los demás `EventoCliente` vencen por su propio `first_received_at` a 30 días. Como
`detalle` es JSON libre, antes de activar la política hay que revisar con datos
de ensayo que no queden otras referencias a pedidos en eventos, logs, cachés o
archivos temporales. La ingesta de un evento tardío con `pedido_id` o UUID ya
purgado sustituye su detalle por un marcador, pero textos sin vínculo
inequívoco siguen siendo un bloqueo hasta auditarlos.

## 0. Preflight y decisión operativa

En el VPS, registra sin datos de clientes el volumen por estado y edad en la
base local productiva. Aún no hay volumen real medido ni decisión del dueño
sobre pedidos `pendiente`, `confirmado` o `enviado` que cumplan 30 días. La purga
real se detiene si alguno resulta elegible; no hay excepción automática al
plazo ni borrado silencioso de un pedido en atención.

```sql
SELECT estado, count(*) AS pedidos,
       count(*) FILTER (WHERE first_received_at IS NULL) AS sin_marca,
       count(*) FILTER (WHERE first_received_at <= now() - interval '30 days') AS vencidos
FROM pedidos_pedido GROUP BY estado ORDER BY estado;
SELECT count(*) AS eventos_sin_marca
FROM pedidos_eventocliente WHERE first_received_at IS NULL;
```

Confirma migraciones `0013` y `0014` en una copia PostgreSQL **local** aislada
antes del corte. `0014` exige `RETENTION_LOCAL_DB_CONFIRMED=True` sólo durante
esa migración y comprueba loopback/socket; nunca la ejecutes sobre el origen
externo. La
`0014` instala triggers PostgreSQL en `Pedido` y `EventoCliente`: en `INSERT`
fijan la marca con el reloj del servidor; una marca ya fijada no puede cambiar
por `UPDATE`. Los registros
históricos restaurados **antes de aplicar la migración** quedan `NULL`. No
despliegues ese trigger sobre el origen externo para fingir recepción en el
VPS. El comando de backfill sólo acepta una fecha documentada por evidencia
de la primera carga al VPS; si staging previo impide conocerla, conserva `NULL`
y pide una decisión explícita. El backfill se puede dirigir a `pedido`,
`evento` o `ambos`, pero en cada modelo actualiza **todos** sus `NULL`; si la
evidencia indica varias cargas, hace falta un procedimiento acotado y aprobado.
Un restore posterior debe preservar las marcas existentes, nunca reiniciarlas;
restaura en una base vacía aislada y verifica las marcas antes de abrir tráfico.

Todas las llamadas de Django siguientes se ejecutan con el release aprobado
y los secretos ya instalados fuera de Git. No ejecutes `source` ni `.` sobre un
`.env` real. El ejemplo usa la unidad transitoria para delegar su lectura a
systemd; ajusta la unidad a staging y al entorno aislado durante el ensayo:

```bash
APP_ROOT=/home/deploy/apps/tcysPedidosSucursales
RELEASE=$(readlink -f "$APP_ROOT/current")
RETENTION_ENV="$APP_ROOT/shared/.env"
test -d "$RELEASE"
test -x "$RELEASE/.venv/bin/python"
test -f "$RETENTION_ENV" && test -r "$RETENTION_ENV"
test "$(stat -c '%U:%G' "$RETENTION_ENV")" = deploy:deploy
test "$(stat -c '%a' "$RETENTION_ENV")" = 600
```

Para históricos, el dry-run no escribe. La fecha y `evidencia` deben salir del
inventario, no de `fecha_creacion` o `fecha_confirmacion`:

```bash
sudo systemd-run --unit=tcys-retencion-backfill-check --wait --pipe --collect \
  --property=Type=oneshot --property=User=deploy --property=Group=deploy \
  --property="WorkingDirectory=$RELEASE" \
  --property="EnvironmentFile=$RETENTION_ENV" \
  "$RELEASE/.venv/bin/python" manage.py backfill_first_received_at \
  --modelo=ambos --fecha-recepcion='<UTC_APROBADA_ISO8601>' \
  --evidencia='<REFERENCIA_NO_SENSIBLE>'
```

La aplicación real requiere otra ventana y `--apply` junto con
`RETENTION_BACKFILL_ENABLED=True` sólo en esa unidad; registra conteos antes y
después. No se ejecuta como parte automática de `migrate`.

## 1. Generar el ZIP de retención

Usa un directorio privado fuera del release y del repositorio, propietario
`deploy`, modo `0700`, en un volumen contabilizado en la política de copias.
El comando limita a 500 pedidos por lote por defecto; divide la ventana si lo
excede. Si el backfill dio el mismo instante a muchos históricos, divide
además por intervalos disjuntos `--id-desde` inclusivo y `--id-hasta` exclusivo
sin superar 5 000 por lote. Comprueba que la unión de manifiestos cubra todos
los IDs esperados sin duplicados. La selección es por recepción en el VPS, no por fecha de negocio. El
ZIP 0600 contiene `manifest.json` y `pedidos.jsonl` con IDs, UUID, alcance,
conteos, versión, hora UTC y SHA-256 del contenido. La salida del comando
muestra lote y SHA-256 del archivo, nunca el contenido.

```bash
install -d -m 0700 /home/deploy/exports-pedidos-privado
ARCHIVO=/home/deploy/exports-pedidos-privado/lote-<UUID_NUEVO>.zip
test ! -e "$ARCHIVO"
sudo systemd-run --unit=tcys-retencion-export --wait --pipe --collect \
  --property=Type=oneshot --property=User=deploy --property=Group=deploy \
  --property="WorkingDirectory=$RELEASE" \
  --property="EnvironmentFile=$RETENTION_ENV" \
  "$RELEASE/.venv/bin/python" manage.py generar_exportacion_retencion \
  --desde-recepcion='<UTC_INICIO_ISO8601>' \
  --hasta-recepcion='<UTC_FIN_ISO8601>' --archivo="$ARCHIVO"
```

Generar el ZIP **no** confirma la descarga y no habilita purga. No subas
archivos reales a Git ni los dejes en `/tmp` o en `releases/`.

## 2. Copiar, verificar y confirmar

Copia el archivo al equipo autorizado por el canal aprobado. Calcula SHA-256
en **ese equipo**, compáralo con el hash del ticket, y verifica que allí se
pueda abrir el ZIP y leer el manifiesto. Registra operador, equipo y referencia
de recepción en un acta privada. Un HTTP 200, una descarga iniciada o marcar
un pedido `ENVIADO` no equivalen a esa confirmación. En este checkout ni
siquiera existe la ruta `/admin/pedidos/<id>/descargar/` citada en un prompt
anterior; impresión y cambio de estado están separados.

Tras verificar el destino, ejecuta con el SHA calculado allí:

```bash
sudo systemd-run --unit=tcys-retencion-confirm --wait --pipe --collect \
  --property=Type=oneshot --property=User=deploy --property=Group=deploy \
  --property="WorkingDirectory=$RELEASE" \
  --property="EnvironmentFile=$RETENTION_ENV" \
  "$RELEASE/.venv/bin/python" manage.py confirmar_exportacion_retencion \
  '<LOTE_UUID>' --archivo-local="$ARCHIVO" \
  --sha256-destino='<SHA256_VERIFICADO_EN_DESTINO>' \
  --referencia='<ACTA_NO_SENSIBLE>' --integridad-confirmada
```

El comando vuelve a calcular el hash local y la firma de cada pedido. Si el
archivo es parcial, el hash difiere, falta un pedido o cambió algún campo,
falla y exige un nuevo export. Cuando confirma, elimina el ZIP local y registra
la confirmación por separado del estado comercial. Verifica que no quede otra
copia del lote en dumps, snapshots o temporales bajo control del VPS.

La confirmación tiene dos fases: primero fija `CONFIRMADA`; después elimina
el ZIP y registra `archivo_local_eliminado_en`. Si la segunda fase falla, el
lote no es purgable. Tras inspección, reintenta con
`manage.py limpiar_exportacion_retencion <LOTE_UUID> --apply`; si el archivo
ya falta y verificaste su ausencia, añade `--permitir-ausente`. Un ticket
`GENERADA` de más de 24 horas o `INVALIDADA` puede inspeccionarse con el mismo
comando sin `--apply`. Su limpieza real exige
`RETENTION_EXPORT_CLEANUP_ENABLED=True` **sólo en la unidad transitoria
autorizada**. No instales timer de limpieza; retira también otras copias del
ZIP en backups/temporales del VPS.

## 3. Dry-run y purga manual autorizada

```bash
sudo systemd-run --unit=tcys-retencion-dry-run --wait --pipe --collect \
  --property=Type=oneshot --property=User=deploy --property=Group=deploy \
  --property="WorkingDirectory=$RELEASE" \
  --property="EnvironmentFile=$RETENTION_ENV" \
  "$RELEASE/.venv/bin/python" manage.py purgar_retencion
```

La salida JSON incluye IDs técnicos y conteos de pedidos por edad/exportación,
pedidos abiertos bloqueados, pedidos/eventos históricos sin marca, tickets no confirmados o
obsoletos, ítems dependientes, macropedidos que quedarían vacíos y eventos.
Repite el dry-run: el conjunto debe ser idéntico si nadie cambió datos. Un
pedido modificado tras el export no entra por confirmación; reexporta. Un
pedido abierto vencido bloquea toda ejecución real y exige la decisión
operativa documentada. Revisa FK y los backups antes de pasar a `--apply`.

**Sólo tras autorización final específica**, en ventana controlada, se puede
ejecutar el mismo comando con `--apply` y el override de esa ejecución
`RETENTION_PURGE_ENABLED=True`. No cambies el `.env` persistente a `True` ni
instales un timer hasta que la matriz, staging, restauración y alertas pasen
las pruebas. La purga crea `RegistroPurga` y un `PedidoPurgado` mínimo por
UUID; no deja cuerpos de pedidos en los recibos. La segunda ejecución no debe
volver a borrar ni generar otro registro. Registra duración, filas por tabla,
errores, dato más antiguo y próxima revisión sin nombres ni payloads.

## 4. Datos vencidos, backups y restore

No mantengas dumps completos por largo plazo. Un backup permanente selectivo
puede contener esquema y datos de maestros, usuarios, configuración y los
recibos mínimos (`ExportacionRetencion`, `RegistroPurga`, `PedidoPurgado`),
excluyendo datos de `Pedido`, `ItemPedido`, `MacroPedido`, `EventoCliente`,
`SesionActiva`, `LogRecordatorio`, `PedidoEnExportacion`, `django_session` y
`django_admin_log`. Verifica el inventario real de tablas antes de fijar los
`--exclude-table-data`; una tabla nueva no debe entrar inadvertidamente en
un backup permanente. Prueba la restauración de ese archivo en una base
aislada. Copia el libro técnico de tombstones/confirmaciones a un destino
duradero autorizado después de cada purga con
`manage.py exportar_ledger_retencion --destino=<ARCHIVO_NUEVO_JSONL>`; verifica
el `.sha256` **en el destino** y guarda el hash esperado en un acta
independiente. Compara conteos y último ID antes de usar un backup anterior.
Un sidecar almacenado junto al JSONL no prueba autenticidad sin canal
autenticado. Los backups transitorios deben desaparecer del VPS
en el primero entre confirmación de export o día 30, igual que sus datos; si
un proveedor no permite retirar snapshots que contienen filas purgadas, no
actives la política hasta acordar otra topología de backup.

Si necesitas restaurar un backup **completo** temporal, mantén la base aislada
y sin tráfico. Incorpora el libro duradero más reciente de tombstones con
`manage.py importar_ledger_retencion --origen=<JSONL> --sha256-esperado=<SHA_DEL_DESTINO> --apply`
y `RETENTION_RESTORE_ISOLATED=True` únicamente en esa base. Antes
de abrir tráfico, ejecuta el dry-run de reconciliación; `--apply` sólo puede
usarse con `RETENTION_RESTORE_ISOLATED=True` y elimina UUID reintroducidos.
Ejecuta también el dry-run de edad, revisa los abiertos y elimina contenido
vencido aprobado. Comprueba conteos/UUID/FK/secuencias y que no queden
pedidos purgados o vencidos accesibles. Una copia completa que carece del libro
de purgas recientes **no** puede abrirse: la edad sola no detecta pedidos
exportados y purgados antes de 30 días. Audita igualmente snapshots del
proveedor externo y de Hostinger durante el corte a PostgreSQL local.

```bash
sudo systemd-run --unit=tcys-retencion-restore-check --wait --pipe --collect \
  --property=Type=oneshot --property=User=deploy --property=Group=deploy \
  --property="WorkingDirectory=$RELEASE" \
  --property="EnvironmentFile=<ENV_DE_BASE_RESTAURADA_AISLADA>" \
  "$RELEASE/.venv/bin/python" manage.py conciliar_restauracion_retencion
```

## 5. Fallo e indicadores

Si export/confirmación falla, conserva el ticket sin confirmar y no purgues
ese lote; retira el archivo parcial bajo el procedimiento autorizado. Si la
purga falla, la transacción revierte pedidos, dependencias y recibos juntos;
conserva el log técnico sin payload y repite el dry-run. Si el ZIP fue
confirmado pero otro snapshot aún guarda los pedidos, elimina o reconcilia
esa copia antes de autorizar la purga real.

Propón alerta si no hubo revisión/purga en la periodicidad aprobada, si hay
pedidos transitorios >30 días, si crece el conjunto sin `first_received_at` o
si la exportación queda en estado GENERADA sin confirmar. El registro de
operación debe guardar última ejecución, modo, lotes evaluados, filas por tipo,
errores, duración, próxima ejecución y fecha de dato más antiguo. Nunca
registre nombres, direcciones, teléfonos, secretos, `DATABASE_URL`, tokens ni
contenido de pedidos.
