# Migración de Pedidos: PostgreSQL externo → PostgreSQL local del VPS

Estado: **procedimiento propuesto; no ejecutado ni autorizado para producción**.
El resultado buscado es que `tcysweb-prod` use una base **exclusiva de Pedidos**
en el VPS. La base del backend central POS es otra base, con rol, secretos,
backups y ciclo de cambios separados. El staging PostgreSQL local existente no
demuestra que se haya cortado la base productiva.

Este runbook complementa `deploy.md`, `rollback.md` y `api_pos.md`. No se debe
usar la fase 6 de `deploy.md` como sustituto del corte de base: su rollback sólo
revierte código/unidad y perdería pedidos nuevos si se devolviera
`DATABASE_URL` al origen después de abrir escrituras locales.

## 0. Condiciones y parámetros que deben cerrarse antes del ensayo

Registra en una hoja de operación privada, sin valores de credenciales:

| Parámetro | Valor a comprobar en el VPS/proveedor |
| --- | --- |
| Ref de aplicación y migraciones | SHA completo, `showmigrations --plan`, versión Django y del lock. |
| Origen | Proveedor, versión PostgreSQL, endpoint **directo o de sesión** apto para `pg_dump`, conectividad TLS y política de snapshots. |
| Destino | Versión PostgreSQL, base y rol exclusivos de Pedidos, host `127.0.0.1`/socket, `pg_hba.conf`, disco libre y backup. |
| Runtime | Unidad `tcysweb-prod`, ruta real de sus `EnvironmentFile`, `current`, otros Gunicorn/Render y todos los escritores. |
| Capacidad | Bytes y duración del dump, restore, validación y backup; ventana máxima aprobada de bloqueo de escrituras. |
| Datos | Tablas, migraciones, extensiones, collation, zona horaria, índices, FK, secuencias, número de pedidos abiertos y `first_received_at` históricos/desconocidos. |
| POS | Sucursales autorizadas, mapa `SucursalCliente.id` ↔ identidad POS, consumidores, cursores y versión del contrato. |
| Retención | Copias y snapshots en proveedor y Hostinger, responsables, plazo de borrado y registro duradero de confirmaciones/purgas. |

No presupongas que el origen continúa en Supabase ni que el checkout activo
continúa en un SHA citado por un runbook antiguo. Comprueba el estado real sin
mostrar `DATABASE_URL`, archivos de entorno, contraseñas o datos de filas. No
ejecutes `source`/`.` de un `.env` (sus valores no son necesariamente sintaxis
de Bash). No pongas un URI con contraseña en argumentos de `psql`, `pg_dump`,
`pg_restore`, logs, tickets o Git.

Para los ejemplos siguientes, el operador prepara **sólo en el VPS** un
`PGSERVICEFILE` y un `PGPASSFILE` con propietario autorizado y modo `0600`.
Los servicios `tcys_pedidos_origen`, `tcys_pedidos_ensayo` y
`tcys_pedidos_local` contienen conexiones distintas; `pg_dump` usa un rol de
lectura del origen, el ensayo sólo puede escribir en una base aislada, y la
producción local tiene un rol de aplicación de mínimo privilegio. El archivo de
servicio puede referir al passfile; no copies ningún archivo a releases o Git.
Configura TLS del origen según el certificado y requisitos reales del proveedor
(preferentemente `DB_SSLMODE=verify-full` con CA validada); documenta por
separado la conexión local por loopback/socket, que puede requerir
`DB_SSLMODE=disable` en el entorno privado de la unidad de Pedidos. El ajuste
explícito prevalece sobre `sslmode` de `DATABASE_URL`; en producción, si ninguno
lo define, el valor predeterminado sigue siendo `require`. `DEBUG` no decide
la política TLS de base. Comprueba la conexión remota con `pg_stat_ssl` y la
local con `inet_server_addr()`/`pg_stat_ssl`, sin imprimir URLs ni secretos.
El PostgreSQL local no debe escuchar en interfaces públicas ni abrir `5432` en
UFW. No uses la base del backend central POS.

Consultas de inventario permitidas, con conexión ya provisionada (no muestran
secretos ni filas de negocio):

```bash
export PGSERVICEFILE=/home/deploy/secrets/tcys-pedidos-pg_service.conf
export PGPASSFILE=/home/deploy/secrets/tcys-pedidos.pgpass
test -f "$PGSERVICEFILE" && test -r "$PGSERVICEFILE"
test -f "$PGPASSFILE" && test -r "$PGPASSFILE"
test "$(stat -c '%a' "$PGSERVICEFILE")" = 600
test "$(stat -c '%a' "$PGPASSFILE")" = 600
test "$(stat -c '%U' "$PGSERVICEFILE")" = deploy
test "$(stat -c '%U' "$PGPASSFILE")" = deploy
pg_dump --version
pg_restore --version
psql 'service=tcys_pedidos_origen' -X -Atc 'SHOW server_version;'
psql 'service=tcys_pedidos_origen' -X -Atc 'SHOW TimeZone;'
psql 'service=tcys_pedidos_origen' -X -Atc 'SHOW lc_collate;'
psql 'service=tcys_pedidos_origen' -X -Atc 'SELECT pg_database_size(current_database());'
psql 'service=tcys_pedidos_origen' -X -Atc "SELECT extname, extversion FROM pg_extension ORDER BY extname;"
psql 'service=tcys_pedidos_origen' -X -Atc "SELECT app, name FROM django_migrations ORDER BY app, name;"
psql 'service=tcys_pedidos_origen' -X -Atc "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid();"
psql 'service=tcys_pedidos_local' -X -Atc 'SHOW server_version;'
psql 'service=tcys_pedidos_local' -X -Atc 'SHOW listen_addresses;'
sudo ss -H -lnt 'sport = :5432'
sudo ufw status verbose
sudo systemctl list-timers --all
```

Complementa con inventario de tablas/índices/FK/secuencias, permisos, uso de
disco, tareas cron y escritores externos; no incluyas texto de consultas activas
ni `systemctl show -p Environment`, que podrían exponer credenciales. Verifica
la versión del cliente `pg_dump` frente a la del servidor antes de copiar. La
fuente de verdad para todos los escritores incluye formularios de sucursales,
admin, Render, API, procesos Gunicorn, tareas manuales y cualquier integración
directa; que `SCHEDULER_ENABLED=False` en el código no prueba que no haya cron o
timers en el VPS.

## 1. Ensayo aislado y reproducible

Requiere aprobación de acceso a una **copia con datos reales** y una base de
ensayo separada, sin tráfico público, correo real ni recordatorios. Registra
SHA de código, hora UTC, versión de herramientas, tamaño de origen, espacio
libre, tiempo de cada etapa y destino de artefactos. No ejecutes `seed_demo`,
`flush` ni migraciones destructivas sobre origen o producción.

1. Crea una base de ensayo **vacía** con propietario/migrador propio. Revisa
   extensiones y collation antes de restaurar. Comprueba que los servicios de
   libpq apuntan a bases diferentes. Cierra cualquier conexión de ensayo
   anterior; no uses `--clean` sobre una base existente sin validar su identidad.
2. Crea un directorio **nuevo y vacío** por ensayo fuera del release/Git, bajo
   `/home/deploy/migration-private/`, con modo `0700`. Ejecuta el script
   versionado `deploy/vps/scripts/ensayar_copia_postgresql.sh`. Rechaza
   servicios iguales, destino no local/no vacío o sin nombre de ensayo,
   conexiones concurrentes y archivos de conexión fuera de modo `0600`.
   El script hace un dump custom, SHA-256, restore en una transacción y
   manifiesto del destino; no usa `--clean`, `DROP` ni credenciales en
   argumentos:

   ```bash
   umask 077
   install -d -m 0700 /home/deploy/migration-private
   install -d -m 0700 /home/deploy/migration-private/ensayo-AAAAMMDD-01
   bash deploy/vps/scripts/ensayar_copia_postgresql.sh \
     --source-service tcys_pedidos_origen \
     --target-service tcys_pedidos_ensayo \
     --output-dir /home/deploy/migration-private/ensayo-AAAAMMDD-01
   ```

   Sustituye `AAAAMMDD-01` por un identificador propio del ensayo. El
   directorio se mantiene privado y **no se reutiliza** para un retry; si el
   restore falla, diagnostica el estado del destino antes de crear una nueva
   base vacía. `--single-transaction` evita un restore parcial; en bases
   grandes verifica si consumo de locks y duración son viables. El SHA-256
   comprueba integridad del archivo, **no** igualdad lógica de las bases.
3. Valida `resultado.txt`, `destino-manifiesto.txt` y el diff de esquema
   generados por el script. No contienen payload de pedidos, pero mantenlos
   bajo acceso privado. Revisa `last_value` de secuencias con un rol autorizado
   si aparece `NULL`; verifica que la siguiente asignación no colisionará con
   `max(id)` sin ejecutar `nextval` en el origen.

4. Ejecuta `manage.py migrate --plan` sobre ensayo con `EnvironmentFile` de
   staging; inspecciona cada operación. Sólo después aplica migraciones
   **compatibles** a esa copia. El histórico incluye una migración de datos
   (`pedidos.0012`) que consolida pedidos pendientes y toma un lock en
   PostgreSQL; no debe reaplicarse por error sobre la fuente. Desactiva envío
   SMTP y recordatorios; conserva el orden de `EnvironmentFile` base,
   overrides y override de ensayo con `DATABASE_URL` de ensayo al final. Nunca
   cargues esos archivos desde Bash. La migración `0014` exige
   `RETENTION_LOCAL_DB_CONFIRMED=True` **sólo en la ejecución migradora** y
   además comprueba loopback/socket PostgreSQL. Déjalo `False` en entornos
   persistentes y en el origen externo: no se debe marcar allí una falsa
   primera recepción al VPS.
5. Con las mismas credenciales de sólo lectura, verifica en ensayo la versión
   de migraciones, lista de tablas/extensiones, conteo exacto y `min(id)`/
   `max(id)` de **cada tabla**, agregados monetarios de `Pedido` y
   `ItemPedido`, UUID únicos, FK validadas, índices, propietario/permisos y
   `last_value` de cada secuencia frente a `max(id)` (`is_called` se revisa
   por separado con el rol autorizado si hace falta). No avances secuencias
   con `nextval` como comprobación. La cuenta del rol web debe
   permitir sólo su DML necesario; el rol de migración se usa separadamente.
   El SQL versionado `deploy/vps/scripts/manifesto_postgresql.sql` sólo
   muestra metadatos técnicos: tablas, IDs mínimos/máximos, conteos,
   agregados, FK y secuencias. Ejecútalo en **ambos** servicios y compara sus
   salidas durante el corte con el origen congelado. En ensayo con origen
   activo su salida posterior al dump sirve de diagnóstico, no de igualdad
   exacta:

   ```bash
   psql --no-psqlrc --no-password --set=ON_ERROR_STOP=1 \
     --dbname='service=tcys_pedidos_origen' \
     --file=deploy/vps/scripts/manifesto_postgresql.sql
   psql --no-psqlrc --no-password --set=ON_ERROR_STOP=1 \
     --dbname='service=tcys_pedidos_ensayo' \
     --file=deploy/vps/scripts/manifesto_postgresql.sql
   ```

   Si `last_value` aparece `NULL` por falta de permiso, usa el rol autorizado
   para verificarlo; no interpretes `NULL` como cero.
6. Prueba con datos **sintéticos en ensayo** captura y confirmación, lectura
   paginada de `/api/v2/pos/pedidos/`, impresión operativa y comando de
   recordatorios en modo de prueba con correo en memoria. Las rutas reales son
   `GET /admin/pedidos/<id>/imprimir/` y
   `POST /admin/pedidos/<id>/marcar-enviado/`; ninguna confirma la descarga de
   un export de retención. No existe `/admin/pedidos/<id>/descargar/` en este
   checkout.
   Revisa la correspondencia de sucursales, incluidos IDs inexistentes o
   ambiguos. El ensayo no debe exponer PostgreSQL ni abrir otro puerto público.
7. Documenta la limpieza autorizada de dump/base de ensayo y snapshots. Un
   dump real contiene datos transitorios: se contabiliza en la política de
   retención, se mantiene privado y no se conserva indefinidamente.

El origen puede seguir recibiendo escrituras durante este ensayo. En ese caso
los conteos obtenidos **después** del dump no comparten su snapshot y no son
una prueba exacta origen→destino. Para una comparación exacta usa el mismo
snapshot exportado para dump y manifiesto, o una pausa de escritores aprobada;
en el corte final la pausa es obligatoria. Anota cualquier diferencia, no la
atribuyas automáticamente a «actividad normal».

## 2. Preflight del corte productivo

El responsable fija por escrito, a partir del ensayo, duración máxima de
congelamiento, método de bloqueo **comprobado** de todos los escritores,
responsable de abortar, alcance de snapshots/backup y ventana de reversión.
Hasta que haya una medición, el límite provisional es **15 minutos** desde el
bloqueo de escrituras hasta decidir reabrirlas; cambiarlo exige aprobarlo
**antes** del corte, nunca por presión durante la incidencia.

Condiciones de entrada:

- `tcysweb-prod` sano, `127.0.0.1:8002` escuchando, base de origen identificada
  sin imprimir URI; respaldo de origen verificable por restore de ensayo.
- Base **productiva local de Pedidos vacía** e inequívocamente distinta de
  staging y de la base central POS; `listen_addresses`/UFW restringen 5432;
  espacio libre de al menos **2× el dump medido más 25 %** para margen de
  restore/WAL, o capacidad mayor aprobada según el ensayo.
- Migraciones y ref de aplicación fijados; `first_received_at` existente o
  plan de backfill revisado. La fecha de migración no reinicia esa marca.
- Purga automática apagada. La API POS y sus cursores tienen plan de
  conciliación; no se afirma disponibilidad histórica ilimitada.
- Se ha ensayado el mecanismo de bloqueo de escrituras, incluido un intento
  controlado que debe **fallar**; no basta poner una página de mantenimiento
  si quedan procesos con acceso a PostgreSQL.
- Entorno local privado preparado con `DATABASE_URL` explícita, política SSL
  local revisada y rollback de unidad/configuración. No sustituyas sólo
  `current`: el cambio de base es un paso aparte y registrado.

## 3. Corte ordenado: secuencia bajo autorización final

1. Registra hora UTC y estado de origen/destino; avisa la ventana al operador
   POS. Cierra o pausa capturas web, admin, Render, cron/timers, consumidores y
   conexiones directas. Comprueba ausencia de nuevos commits de escritura en
   origen mediante el mecanismo de bloqueo ensayado; mantiene lectura sólo si
   no compromete el backup. El scheduler embebido está retirado, pero comprueba
   procesos y timers reales antes y después.
2. Con el origen ya congelado, toma backup final consistente en archivo
   privado, calcula SHA-256 y verifica restaurabilidad. Conserva el origen sin
   escrituras durante toda la ventana de reversión. Registra manifest de
   origen **del mismo estado congelado**: conteo exacto por tabla, `min/max(id)`,
   suma de `Pedido.total` y `ItemPedido.subtotal`, UUID únicos, migraciones,
   secuencias y FK. No incluyas filas, nombres, teléfonos ni payloads en el
   informe compartido.
3. Restaura sólo a la base local exclusiva de Pedidos. Repite el plan de
   migraciones y aplica únicamente las aprobadas sobre la copia local, con
   `RETENTION_LOCAL_DB_CONFIRMED=True` únicamente en el proceso migrador para
   `0014`. Compara
   el manifest; prueba permisos del rol app sin otorgar superusuario. Comprueba
   la política de TLS remoto y conexión local; ningún socket PostgreSQL debe
   aceptar clientes públicos.
4. Arranca únicamente `tcysweb-prod` con el release aprobado y la URL local
   por el mecanismo de `EnvironmentFile` definido para esa unidad. No arranques
   un segundo servicio productivo escritor. Comprueba health, inicio de sesión,
   consulta y paginación POS para sucursal permitida, y 403/401 para acceso
   indebido. Prueba una captura sintética sólo en una ruta de prueba aislada;
   antes de abrir producción, la comprobación productiva debe ser **lectura**.
5. Si todo pasa, abre escrituras una sola vez hacia la base local. Vigila 5xx,
   latencia, errores de base, conexiones, filas nuevas y consumo de disco. El
   origen permanece congelado durante la ventana de reversión; el backend
   central POS conserva su propia base y no cambia en este corte.

**Abortar antes de abrir escrituras** ante cualquiera de estos hechos:

- un escritor del origen sigue activo, o no puede demostrarse bloqueo;
- se supera el límite de 15 minutos (o el límite aprobado previamente);
- error de dump/restore/checksum o versión/esquema/extensión incompatible;
- diferencia **distinta de cero** en conteos por tabla, agregados exactos,
  UUID, migraciones, FK o secuencias no explicada y aprobada;
- falta de `first_received_at`/plan de backfill para históricos que se
  planea purgar, o riesgo de purga/timer accidental;
- fallo de autenticación, filtro por sucursal o paginación de API POS;
- tráfico PostgreSQL desde interfaz pública o rol web con privilegios
  superiores a los aprobados.

Si se aborta antes de la primera escritura local, vuelve a la unidad y
configuración antiguas ya respaldadas, prueba el servicio contra el origen
congelado y **sólo entonces** reabre sus escritores. No restaures la base
externa desde la copia local en este camino. Conserva evidencias privadas y
diagnostica el ensayo.

## 4. Fallo después de aceptar escrituras locales

Desde la primera escritura local, cambiar únicamente `DATABASE_URL` al origen
perdería pedidos o estados nuevos. El rollback de `rollback.md` es sólo de
aplicación y **no** autoriza esa operación de base.

1. Congela **todos** los escritores locales y deja el origen externo congelado.
   Anota último commit/ID/UUID observado, hora UTC y pedidos que ya se
   descargaron o cambiaron de estado. Haz backup verificable de ambas bases
   antes de reparar; evita logs con payloads.
2. Opción normal: **reparación hacia delante**. Conserva la base local como
   fuente de verdad, restaura código/entorno compatible con ese esquema o
   corrige la incidencia en una nueva release. Verifica filas creadas,
   modificadas y borradas, secuencias, API y operaciones de pedidos antes de
   reabrir. Este camino no depende de reconstruir cambios desde logs
   incompletos.
3. Sólo si se preparó y probó previamente una captura completa de cambios,
   puede evaluarse retorno al origen: extrae del local el delta **desde el
   snapshot final** con inserciones, actualizaciones, borrados, relaciones y
   confirmaciones/descargas; aplícalo una sola vez sobre copia aislada del
   origen, resuelve conflictos por `Pedido.codigo_publico` y claves
   dependientes, reconcilia conteos/UUID/FK/secuencias y estados `ENVIADO`/
   `RECIBIDO`, luego restaura al origen bajo ventana controlada. Exige
   demostración de **cero pedidos y cambios perdidos** y aprobación humana
   antes de reabrir allí. La PK entera y el folio de fecha no bastan como
   identidad externa. El repositorio **no tiene hoy** un diario completo de
   cambios ni un script de delta inverso; por tanto esta opción no está lista
   para ejecutarse.

No permitas dos bases con escrituras simultáneas. La retirada del origen
externo sólo se decide después de cerrar la ventana de reversión y conciliar
backups/snapshots con retención.

## 5. Retención, backups y restauración

La migración conserva el `first_received_at` original de pedidos y eventos. Para registros antiguos
sin marca fiable se identifica el conjunto y se pide al dueño una regla de
backfill conservadora; no se supone que `fecha_creacion`, `fecha_confirmacion`
`EventoCliente.recibido_en` o la fecha de restore prueben primera recepción. No se activa la purga durante
el corte ni por la existencia de este runbook.

Separa backup duradero de maestros/configuración/usuarios del backup temporal
de `Pedido`, `ItemPedido`, `MacroPedido`, eventos transaccionales y exports. Un
backup completo de ensayo o corte puede contener datos ya elegibles por
descarga confirmada antes de 30 días; **sólo la edad no basta** para limpiar un
restore. Antes de abrir tráfico a una base restaurada, aísla la copia,
reconcilia con un registro duradero mínimo de exportaciones confirmadas y
purgas, elimina contenido que no puede reaparecer y valida el resultado. El
ledger versionado `pedidos/retencion_ledger.py` permite exportar recibos y
tombstones técnicos en JSONL sin contenido de pedidos mediante
`manage.py exportar_ledger_retencion --destino RUTA_NUEVA`. Genera además un
`.sha256`; ambos archivos se deben copiar a custodia autorizada **fuera de la
base y del VPS**, verificar allí y actualizar tras cada purga. Sin esa copia
duradera no se puede demostrar que un snapshot anterior no reintroducirá
pedidos ya borrados.

Para un restore **aislado**, la secuencia es: validar SHA externo, cargar
`RETENTION_RESTORE_ISOLATED=True` sólo en ese proceso mediante
`EnvironmentFile` (sin hacer `source`), ejecutar
`manage.py importar_ledger_retencion --origen RUTA_JSONL --sha256-esperado SHA256 --apply`,
revisar `pedidos_reintroducidos`, ejecutar
`manage.py conciliar_restauracion_retencion` primero en dry-run y luego con
`--apply` tras aprobación, y exigir cero reintroducciones antes de abrir
tráfico. El import conserva IDs de recibos y falla ante conflictos; **no**
elimina pedidos por sí solo. Audita
las copias/snapshots del proveedor externo, Hostinger, dumps de migración,
temporales y repositorios de backup; fija eliminación conforme a la política
aprobada, sin prometer borrado físico de sectores del proveedor.

El backend central POS aplica una política separada: sus **clientes** son
permanentes; sus ventas transitorias requieren su propio export/hash/ACK y
retención. Ninguna de esas tablas se copia a la base de Pedidos.

## 6. Evidencia y handoff al integrador

Entrega al operador autorizado, sin secretos ni payloads: commit/ref,
versiones, matriz origen→destino de tablas, lista de escritores y método de
bloqueo, tiempos y tamaños medidos, SHA-256 de artefactos privados, manifiesto
de conteos/agregados/FK/secuencias, resultados de prueba funcional y API POS,
umbral de abortar, respaldo verificado, ruta de rollback antes de escrituras,
plan de reparación después de escrituras, plazo de conservación del origen y
responsables de snapshots. Los archivos de evidencia real permanecen fuera de
Git con control de acceso y retención.

Permanecen pendientes de acceso/decisión antes de autorizar el corte: proveedor
y versión exactos del origen, permisos para dump directo y snapshot,
conectividad origen→VPS, tamaño y duración reales, versión/roles/puerto local,
todos los escritores (incluidos Render y jobs), collation/extensiones,
backups de ambos proveedores, mapa de sucursales, estrategia aprobada para
históricos sin `first_received_at`, tratamiento de pedidos abiertos a 30 días,
ventana de mantenimiento, umbral medido y propietario del incidente. Hasta
resolverlos, un ensayo satisfactorio **no** equivale a migración productiva.

Referencias técnicas: documentación oficial de PostgreSQL para
[`pg_dump`](https://www.postgresql.org/docs/current/app-pgdump.html) y
[`pg_restore`](https://www.postgresql.org/docs/current/app-pgrestore.html).
