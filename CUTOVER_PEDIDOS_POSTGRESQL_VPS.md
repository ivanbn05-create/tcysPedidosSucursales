# Corte Pedidos Production 1.0: PostgreSQL externo → PostgreSQL local VPS

Ejecutar **sólo con autorización de corte**, como `deploy`, en una única sesión Bash del VPS. Los comandos no hacen `source` de `.env`, no muestran URL/bearer y no tocan la base del backend central POS. El SHA final se introduce al iniciar; la rama no es una referencia de despliegue. El release debe estar extraído, con venv Python 3.13.12 y lock verificado según `deploy/vps/runbooks/deploy.md` fases 1–3. No ejecutar la fase 4 histórica de ese documento: aquí sí hay migraciones. Mantener v1, el origen externo/Supabase legacy y su configuración durante la ventana de rollback.

## 1. Identificar origen y fijar entradas

```bash
set -euo pipefail
set +x
umask 077
APP_ROOT=/home/deploy/apps/tcysPedidosSucursales
ACTIVE_CHECKOUT=/home/deploy/src/tcysPedidosSucursales
PGSERVICEFILE=/home/deploy/secrets/tcys-pedidos-pg_service.conf
PGPASSFILE=/home/deploy/secrets/tcys-pedidos.pgpass
export PGSERVICEFILE PGPASSFILE
SOURCE='service=tcys_pedidos_origen'
TARGET='service=tcys_pedidos_local'
LOCAL_ENV=/home/deploy/secrets/tcysweb-prod-local-db.env
FREEZE_WRITERS=/home/deploy/secrets/pedidos-freeze-writers.sh
VERIFY_FROZEN=/home/deploy/secrets/pedidos-verify-writers-frozen.sh
RESUME_LOCAL=/home/deploy/secrets/pedidos-resume-local-writers.sh
RESUME_ORIGIN=/home/deploy/secrets/pedidos-resume-origin-writers.sh
POS_CHECK=/home/deploy/secrets/pedidos-pos-cutover-check.sh
read -r -p 'SHA final aprobado (40 hex): ' COMMIT
[[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]]
read -r -p 'Límite de freeze aprobado (segundos): ' FREEZE_LIMIT
[[ "$FREEZE_LIMIT" =~ ^[1-9][0-9]*$ ]]
RELEASE="$APP_ROOT/releases/$COMMIT"
CUT_DIR="/home/deploy/migration-private/cutover-$(date -u +%Y%m%dT%H%M%SZ)-${COMMIT:0:12}"
psql -X -At --no-password -v ON_ERROR_STOP=1 -d "$SOURCE" -c \
  "SELECT current_database(), inet_server_addr(), current_setting('server_version'), (SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid());"
psql -X -At --no-password -v ON_ERROR_STOP=1 -d "$TARGET" -c \
  "SELECT current_database(), inet_server_addr(), current_setting('server_version');"
```

## 2. Prechecks y respaldo de unidad/configuración

```bash
test "$(id -un)" = deploy
for f in "$PGSERVICEFILE" "$PGPASSFILE" "$LOCAL_ENV"; do
  test -f "$f" && test ! -L "$f" && test -r "$f"
  test "$(stat -c '%a:%U' "$f")" = '600:deploy'
done
for f in "$FREEZE_WRITERS" "$VERIFY_FROZEN" "$RESUME_LOCAL" "$RESUME_ORIGIN" "$POS_CHECK"; do
  test -f "$f" && test ! -L "$f" && test -x "$f"
  test "$(stat -c '%U' "$f")" = deploy
done
test -d "$RELEASE" && test -x "$RELEASE/.venv/bin/python"
test "$(git --git-dir="$APP_ROOT/repo.git" rev-parse "$COMMIT^{commit}")" = "$COMMIT"
test -x "$RELEASE/.venv/bin/gunicorn"
test "$("$RELEASE/.venv/bin/python" -c 'import sys; print(sys.version.split()[0])')" = 3.13.12
"$RELEASE/.venv/bin/python" "$RELEASE/deploy/vps/verify_lock.py"
sudo systemctl is-active --quiet tcysweb-prod.service
sudo ss -H -lnt 'sport = :8002' | grep -q '127.0.0.1:8002'
sudo nginx -t
VERIFY_UNIT=$(mktemp /tmp/tcysweb-prod-verify.XXXXXX.service)
sed "s|/home/deploy/apps/tcysPedidosSucursales/current|$RELEASE|g" \
  "$RELEASE/deploy/vps/systemd/tcysweb.service" > "$VERIFY_UNIT"
sudo systemd-analyze verify "$VERIFY_UNIT"
rm -- "$VERIFY_UNIT"
test ! -e /etc/systemd/system/tcysweb-prod.service.d/20-local-postgresql.conf
test -f "$APP_ROOT/shared/.env" && test "$(stat -c '%a:%U' "$APP_ROOT/shared/.env")" = '600:deploy'
test ! -e "$CUT_DIR"
install -d -m 0700 "$CUT_DIR" "$CUT_DIR/incoming" "$CUT_DIR/restore"
sudo install -m 0600 /etc/systemd/system/tcysweb-prod.service "$CUT_DIR/tcysweb-prod.service.before"
if test -L "$APP_ROOT/current"; then
  readlink -f "$APP_ROOT/current" > "$CUT_DIR/previous_release"
  grep -q "^$APP_ROOT/releases/" "$CUT_DIR/previous_release"
  printf 'release\n' > "$CUT_DIR/rollback_mode"
elif test ! -e "$APP_ROOT/current"; then
  git -C "$ACTIVE_CHECKOUT" rev-parse HEAD > "$CUT_DIR/previous_commit"
  printf '%s\n' "$ACTIVE_CHECKOUT" > "$CUT_DIR/previous_release"
  printf 'bootstrap\n' > "$CUT_DIR/rollback_mode"
else
  echo 'current no es symlink' >&2; exit 1
fi
test "$(psql -X -At --no-password -d "$TARGET" -c \
  "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','p','v','m','S') AND n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema';")" = 0
TARGET_HOST=$(psql -X -At --no-password -d "$TARGET" -c \
  "SELECT split_part(coalesce(inet_server_addr()::text,'socket'),'/',1);")
case "$TARGET_HOST" in 127.0.0.1|::1|socket) ;; *) exit 1 ;; esac
```

La conexión `tcys_pedidos_origen` debe ser directa o de sesión y apta para `pg_dump`; `tcys_pedidos_local` debe señalar una base exclusiva y vacía de Pedidos en loopback/socket. El archivo privado `LOCAL_ENV` debe contener la URL local para systemd, no la externa; se prepara y verifica por canal de secretos del operador, nunca en Git. Los cinco scripts privados del operador son gates obligatorios: congelan **todos** los escritores (web, admin, Render, cron/timers, conexiones directas), prueban con una transacción revertida que no se acepta escritura, liberan sólo los escritores locales o externos en el camino correspondiente y verifican el POS real. Si falta uno o no fue ensayado, detener aquí.

## 3. Freeze de escritores y prueba de barrera

```bash
date -u '+freeze_inicio=%Y-%m-%dT%H:%M:%SZ' | tee "$CUT_DIR/tiempos.txt"
FREEZE_DEADLINE=$(( $(date +%s) + FREEZE_LIMIT ))
sudo systemctl stop tcysweb-prod.service
"$FREEZE_WRITERS"
"$VERIFY_FROZEN"
sudo systemctl is-active --quiet tcysweb-prod.service && exit 1 || true
```

## 4. Dump del origen congelado

```bash
pg_dump --no-password --dbname="$SOURCE" --format=custom \
  --no-owner --no-acl --file="$CUT_DIR/incoming/pedidos.dump"
pg_restore --list "$CUT_DIR/incoming/pedidos.dump" > "$CUT_DIR/dump.toc"
psql -X --no-password -v ON_ERROR_STOP=1 -d "$SOURCE" \
  -f "$RELEASE/deploy/vps/scripts/equivalencia_postgresql.sql" > "$CUT_DIR/origen.equivalencia"
```

## 5. SHA-256 y límite de tiempo

```bash
(cd "$CUT_DIR/incoming" && sha256sum pedidos.dump > pedidos.dump.sha256 \
  && sha256sum -c pedidos.dump.sha256)
test "$(date +%s)" -lt "$FREEZE_DEADLINE"
date -u '+dump_fin=%Y-%m-%dT%H:%M:%SZ' | tee -a "$CUT_DIR/tiempos.txt"
```

## 6. Transferencia al staging privado de restore

`pg_dump` del paso 4 ya transfirió el snapshot por la conexión PostgreSQL cifrada origen→VPS. Esta segunda copia local separa recepción y restore y comprueba bytes idénticos.

```bash
install -m 0600 "$CUT_DIR/incoming/pedidos.dump" "$CUT_DIR/restore/pedidos.dump"
cmp "$CUT_DIR/incoming/pedidos.dump" "$CUT_DIR/restore/pedidos.dump"
sha256sum "$CUT_DIR/restore/pedidos.dump"
```

Comparar el hash anterior con `incoming/pedidos.dump.sha256`; no continuar si difiere.

## 7. Restore local y equivalencia previa a migraciones

```bash
test "$(psql -X -At --no-password -d "$TARGET" -c \
  "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','p','v','m','S') AND n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema';")" = 0
pg_restore --no-password --dbname="$TARGET" --no-owner --no-acl \
  --single-transaction --exit-on-error "$CUT_DIR/restore/pedidos.dump"
psql -X --no-password -v ON_ERROR_STOP=1 -d "$TARGET" \
  -f "$RELEASE/deploy/vps/scripts/equivalencia_postgresql.sql" > "$CUT_DIR/local.pre-migracion.equivalencia"
diff -u "$CUT_DIR/origen.equivalencia" "$CUT_DIR/local.pre-migracion.equivalencia"
test "$(date +%s)" -lt "$FREEZE_DEADLINE"
```

## 8. Migraciones 0017–0019 y resto del release congelado

```bash
TARGET_DB=$(psql -X -At --no-password -v ON_ERROR_STOP=1 -d "$TARGET" \
  -c 'SELECT current_database();')
test -n "$TARGET_DB"
run_local_manage() {
  sudo systemd-run --unit="tcys-pedidos-cutover-$(date +%s%N)" --wait --pipe --collect \
    --property=Type=oneshot --property=User=deploy --property=Group=deploy \
    --property="WorkingDirectory=$RELEASE" \
    --property="EnvironmentFile=$APP_ROOT/shared/.env" \
    --property="EnvironmentFile=$LOCAL_ENV" \
    /usr/bin/env "EXPECTED_DB_NAME=$TARGET_DB" \
      RETENTION_LOCAL_DB_CONFIRMED=True RETENTION_PURGE_ENABLED=False \
      SCHEDULER_ENABLED=False \
      EMAIL_BACKEND=django.core.mail.backends.locmem.EmailBackend \
      "$RELEASE/.venv/bin/python" manage.py "$@"
}
run_local_manage shell -c 'import os; from django.db import connection; connection.ensure_connection(); c=connection.cursor(); c.execute("SELECT current_database(), coalesce(inet_server_addr()::text, %s)", ["socket"]); name, host=c.fetchone(); assert name == os.environ["EXPECTED_DB_NAME"] and host.split("/")[0] in ("127.0.0.1", "::1", "socket"), "REFUSE: migration DB is not the isolated local target"'
run_local_manage migrate --plan | tee "$CUT_DIR/migrate.plan"
run_local_manage migrate --noinput
run_local_manage showmigrations pedidos | tee "$CUT_DIR/migraciones.aplicadas"
for n in 0017 0018 0019 0020 0021 0022; do
  grep -q "\[X\] $n" "$CUT_DIR/migraciones.aplicadas"
done
run_local_manage makemigrations --check --dry-run
```

`RETENTION_LOCAL_DB_CONFIRMED=True` se aplica sólo al proceso migrador/comprobador, nunca al origen ni a la unidad persistente. El restore conserva `first_received_at`; no hacer backfill por fecha de restore.

## 9. Secuencias

```bash
psql -X --no-password -v ON_ERROR_STOP=1 -d "$TARGET" \
  -c "SELECT schemaname,sequencename,last_value FROM pg_sequences WHERE schemaname NOT LIKE 'pg_%' ORDER BY 1,2;" \
  > "$CUT_DIR/secuencias.txt"
test "$(psql -X -At --no-password -d "$TARGET" -c \
  "SELECT count(*) FROM pg_sequences WHERE schemaname NOT LIKE 'pg_%' AND last_value IS NULL;")" = 0
psql -X --no-password -v ON_ERROR_STOP=1 -d "$TARGET" \
  -f "$RELEASE/deploy/vps/scripts/verificar_secuencias_postgresql.sql" \
  > "$CUT_DIR/secuencias-verificadas.txt"
```

El script compara `last_value`/`is_called` de cada secuencia serial/identity con `max(id)` y falla si el siguiente ID colisionaría. No ejecuta `nextval` ni altera el origen. Si falla, detener y corregir sólo en destino antes de abrir escrituras.

## 10. Constraints

```bash
test "$(psql -X -At --no-password -d "$TARGET" -c \
  "SELECT count(*) FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname NOT LIKE 'pg_%' AND NOT c.convalidated;")" = 0
```

## 11. Índices

```bash
test "$(psql -X -At --no-password -d "$TARGET" -c \
  "SELECT count(*) FROM pg_index i JOIN pg_class t ON t.oid=i.indrelid JOIN pg_namespace n ON n.oid=t.relnamespace WHERE n.nspname NOT LIKE 'pg_%' AND (NOT i.indisvalid OR NOT i.indisready OR NOT i.indislive);")" = 0
```

## 12. Conteos y manifiesto posterior a migraciones

```bash
psql -X --no-password -v ON_ERROR_STOP=1 -d "$TARGET" \
  -f "$RELEASE/deploy/vps/scripts/manifesto_postgresql.sql" > "$CUT_DIR/local.post-migracion.manifiesto"
psql -X --no-password -v ON_ERROR_STOP=1 -d "$SOURCE" \
  -f "$RELEASE/deploy/vps/scripts/manifesto_postgresql.sql" > "$CUT_DIR/origen.congelado.manifiesto"
"$VERIFY_FROZEN"
test "$(date +%s)" -lt "$FREEZE_DEADLINE"
```

El `diff` exacto del paso 7 es el gate de igualdad del dump. Tras migrar, inspeccionar los conteos y agregados de tablas originales y los cambios esperados de migraciones; no exigir que las tablas nuevas existan en el origen. Si hay diferencia no explicada, abortar antes de escritura local.

## 13. Configurar DATABASE_URL local sin mostrar secretos

```bash
sudo install -d -m 0755 /etc/systemd/system/tcysweb-prod.service.d
sudo install -m 0644 "$RELEASE/deploy/vps/systemd/20-local-postgresql.conf" \
  /etc/systemd/system/tcysweb-prod.service.d/20-local-postgresql.conf
sudo install -m 0644 "$RELEASE/deploy/vps/systemd/tcysweb.service" \
  /etc/systemd/system/tcysweb-prod.service
sudo systemctl daemon-reload
sudo systemd-analyze verify tcysweb-prod.service
test ! -e "$APP_ROOT/current.next"
ln -s "$RELEASE" "$APP_ROOT/current.next"
mv -Tf "$APP_ROOT/current.next" "$APP_ROOT/current"
```

El drop-in carga `LOCAL_ENV` **después** de `shared/.env`: sólo este archivo privado debe sobrescribir `DATABASE_URL` y `DB_SSLMODE`. Conservar sin cambios `shared/.env`, `.env` y overrides originales para rollback. No usar `source`, `systemctl show -p Environment` ni imprimir el archivo.

## 14. Arrancar servicio todavía bajo barrera de escrituras

```bash
run_local_manage check
run_local_manage check --deploy
sudo systemctl start tcysweb-prod.service
sudo systemctl is-active --quiet tcysweb-prod.service
sudo ss -H -lnt 'sport = :8002' | grep -q '127.0.0.1:8002'
```

## 15. Health

```bash
curl --fail --silent --show-error --output /dev/null \
  -H 'Host: tcysweb.lostocayos-lostcys.com.mx' \
  -H 'X-Forwarded-Proto: https' http://127.0.0.1:8002/
sudo nginx -t
```

## 16. API POS v2, sólo GET y alcance aprobado

```bash
read -r -p 'Edge UUID aprobado: ' EDGE_UUID
read -r -p 'IDs remitentes aprobados (CSV exacto): ' SENDER_IDS
read -rsp 'Bearer v2 aprobado: ' POS_TOKEN; printf '\n'
DESDE=$(date -u -d '10 minutes ago' '+%Y-%m-%dT%H:%M:%SZ')
HASTA=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
printf 'header = "Authorization: Bearer %s"\n' "$POS_TOKEN" | \
  curl --config - --fail --silent --show-error --output "$CUT_DIR/pos-v2-respuesta.json" \
    -H "X-POS-Edge-ID: $EDGE_UUID" \
    -H 'X-POS-Branch-ID: 93a42904-4d09-4a50-94cf-2edbf5aa0e51' \
    -H 'Host: tcysweb.lostocayos-lostcys.com.mx' \
    -H 'X-Forwarded-Proto: https' \
    --get --data-urlencode "desde=$DESDE" --data-urlencode "hasta=$HASTA" \
    --data-urlencode "sucursal_id=$SENDER_IDS" --data-urlencode 'limite=1' \
    http://127.0.0.1:8002/api/v2/pos/pedidos/
unset POS_TOKEN
"$RELEASE/.venv/bin/python" -m json.tool "$CUT_DIR/pos-v2-respuesta.json" > /dev/null
```

No imprimir el JSON ni credenciales. Arboledas es el Edge consumidor, no una fila obligatoria `SucursalCliente`; sólo IDs remitentes aprobados explícitamente. V1 continúa disponible.

## 17. Prueba POS real

```bash
"$POS_CHECK"
"$VERIFY_FROZEN"
test "$(date +%s)" -lt "$FREEZE_DEADLINE"
```

El script privado debe validar mapeo Edge/Branch/remitentes, lectura v2 y checkpoint POS sin confirmar ventas ni crear pedidos productivos. Si devuelve error, ejecutar rollback **antes** de abrir escrituras locales.

## 18. Reabrir escrituras sólo hacia PostgreSQL local

```bash
date -u '+escrituras_locales_inicio=%Y-%m-%dT%H:%M:%SZ' | tee -a "$CUT_DIR/tiempos.txt"
"$RESUME_LOCAL"
```

Desde este punto queda prohibido cambiar sólo `DATABASE_URL` al origen. Mantener el origen congelado y v1/Supabase legacy disponibles para consulta/rollback controlado, sin escritores simultáneos.

## 19. Observación

```bash
sudo systemctl is-active --quiet tcysweb-prod.service
sudo ss -H -lnt 'sport = :8002' | grep -q '127.0.0.1:8002'
sudo journalctl -u tcysweb-prod.service --since '10 minutes ago' \
  --no-pager -o cat > "$CUT_DIR/journal-private.txt"
if grep -Eq 'ERROR|CRITICAL|Traceback' "$CUT_DIR/journal-private.txt"; then
  echo 'Revisar el journal privado antes de continuar' >&2
  exit 1
fi
psql -X -At --no-password -d "$TARGET" -c \
  "SELECT count(*), coalesce(max(id),0) FROM pedidos_pedido;"
"$VERIFY_FROZEN"
```

No incluir payloads, tokens ni URLs de base en el acta compartida. Conservar dump, hashes, manifiestos y backups en custodia privada bajo la política de retención aprobada.

## 20. Rollback: escoger según primera escritura local

**Antes de cualquier escritura local de negocio** (incluye fallo de pasos 1–17; las migraciones sobre el destino no cuentan como pedido nuevo):

```bash
sudo systemctl stop tcysweb-prod.service || true
sudo rm -f -- /etc/systemd/system/tcysweb-prod.service.d/20-local-postgresql.conf
sudo install -m 0644 "$CUT_DIR/tcysweb-prod.service.before" \
  /etc/systemd/system/tcysweb-prod.service
if test "$(cat "$CUT_DIR/rollback_mode")" = release; then
  PREVIOUS=$(cat "$CUT_DIR/previous_release")
  case "$PREVIOUS" in "$APP_ROOT"/releases/*) ;; *) exit 1 ;; esac
  ln -s "$PREVIOUS" "$APP_ROOT/current.rollback"
  mv -Tf "$APP_ROOT/current.rollback" "$APP_ROOT/current"
else
  test "$(cat "$CUT_DIR/previous_release")" = "$ACTIVE_CHECKOUT"
  test "$(git -C "$ACTIVE_CHECKOUT" rev-parse HEAD)" = \
    "$(cat "$CUT_DIR/previous_commit")"
  test -L "$APP_ROOT/current"
  rm -- "$APP_ROOT/current"
fi
sudo systemctl daemon-reload
sudo systemctl start tcysweb-prod.service
sudo systemctl is-active --quiet tcysweb-prod.service
sudo ss -H -lnt 'sport = :8002' | grep -q '127.0.0.1:8002'
curl --fail --silent --show-error --output /dev/null \
  -H 'Host: tcysweb.lostocayos-lostcys.com.mx' \
  -H 'X-Forwarded-Proto: https' http://127.0.0.1:8002/
"$RESUME_ORIGIN"
```

**Después de una primera escritura local:** el rollback de URL anterior **no aplica**. No ejecutar el bloque previo. Congelar todos los escritores locales, conservar el origen congelado, tomar dump+SHA de ambas bases, identificar delta por `codigo_publico` y dependencias, y preferir reparación hacia delante manteniendo la base local como fuente de verdad. No hay script de delta inverso completo en este repo; retornar al origen exige conciliación de inserciones, cambios y borrados, cero pérdidas demostradas y aprobación humana. Comandos iniciales de contención:

```bash
sudo systemctl stop tcysweb-prod.service
"$FREEZE_WRITERS"
"$VERIFY_FROZEN"
pg_dump --no-password --dbname="$TARGET" --format=custom --no-owner --no-acl \
  --file="$CUT_DIR/local-after-writes.dump"
pg_dump --no-password --dbname="$SOURCE" --format=custom --no-owner --no-acl \
  --file="$CUT_DIR/origen-still-frozen.dump"
sha256sum "$CUT_DIR/local-after-writes.dump" "$CUT_DIR/origen-still-frozen.dump" \
  > "$CUT_DIR/incident-dumps.sha256"
```

No reabrir ninguna base hasta completar repair-forward o una reconciliación inversa aprobada. Nunca mantener dos bases con escritores simultáneos.
