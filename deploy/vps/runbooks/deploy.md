# Release por commit

Las fases 1 a 5 construyen y prueban un release en paralelo. No cambian
`current`, no reinician `tcysweb-prod` y no sustituyen el servicio activo. La
fase 6 requiere autorizacion explicita.

## 1. Seleccionar y verificar el commit

Define un SHA-1 completo revisado, nunca una rama flotante:

```bash
APP_ROOT=/home/deploy/apps/tcysPedidosSucursales
REPO_CACHE="$APP_ROOT/repo.git"
ACTIVE_CHECKOUT=/home/deploy/src/tcysPedidosSucursales
BASE_ENV="$ACTIVE_CHECKOUT/.env"
PROD_OVERRIDES_ENV=/home/deploy/secrets/tcysweb-prod-overrides.env
COMMIT=<SHA_DE_40_CARACTERES>
RELEASE="$APP_ROOT/releases/$COMMIT"
PYTHON=/home/deploy/.local/share/uv/python/cpython-3.13.12-linux-x86_64-gnu/bin/python3.13

validar_archivo_entorno() {
  local archivo=$1
  test -f "$archivo" || { echo "Falta el archivo de entorno: $archivo" >&2; exit 1; }
  test -r "$archivo" || { echo "El archivo de entorno no es legible: $archivo" >&2; exit 1; }
  test "$(stat -c '%U:%G' "$archivo")" = "deploy:deploy" || {
    echo "Propietario/grupo inesperado en $archivo" >&2
    exit 1
  }
  test "$(stat -c '%a' "$archivo")" = "600" || {
    echo "Permisos inesperados en $archivo; se requiere 600" >&2
    exit 1
  }
}

validar_archivo_entorno "$BASE_ENV"
validar_archivo_entorno "$PROD_OVERRIDES_ENV"

test "${#COMMIT}" -eq 40
case "$COMMIT" in *[!0-9a-f]*) exit 1 ;; esac
git --git-dir="$REPO_CACHE" fetch origin "$COMMIT"
test "$(git --git-dir="$REPO_CACHE" rev-parse "$COMMIT^{commit}")" = "$COMMIT"
```

El preflight sólo consulta existencia y metadatos de los archivos; no imprime
ni copia sus valores. El orden es significativo: el archivo de overrides se
carga después del `.env` base.

La obtencion es por commit exacto. El checkout servido nunca ejecuta
`git pull`.

## 2. Extraer e instalar el lock

```bash
test ! -e "$RELEASE"
install -d -m 0750 "$RELEASE"
git --git-dir="$REPO_CACHE" archive "$COMMIT" | tar -x -C "$RELEASE"
"$PYTHON" -m venv "$RELEASE/.venv"
"$RELEASE/.venv/bin/python" -m pip install --no-deps -r "$RELEASE/deploy/vps/requirements.lock"
"$RELEASE/.venv/bin/python" -m pip check
"$RELEASE/.venv/bin/python" "$RELEASE/deploy/vps/verify_lock.py"
```

El verificador evalua marcadores de plataforma, compara cada pin y rechaza
paquetes runtime inesperados. `tzdata` se instala solo en Windows; Linux usa la
base de zonas horarias del sistema.

## 3. Pruebas sin Supabase

La suite usa SQLite de test, backend de correo en memoria y un entorno vacio.
`env -i` evita heredar credenciales o configuracion del operador y no carga
ningun `.env`:

```bash
cd "$RELEASE"
test ! -e "$RELEASE/.env"
/usr/bin/env -i \
  PATH=/usr/bin:/bin \
  DJANGO_ENV=test \
  DEBUG=True \
  DATABASE_URL= \
  EMAIL_BACKEND=django.core.mail.backends.locmem.EmailBackend \
  SCHEDULER_ENABLED=False \
  "$RELEASE/.venv/bin/python" manage.py check
/usr/bin/env -i \
  PATH=/usr/bin:/bin \
  DJANGO_ENV=test \
  DEBUG=True \
  DATABASE_URL= \
  EMAIL_BACKEND=django.core.mail.backends.locmem.EmailBackend \
  SCHEDULER_ENABLED=False \
  "$RELEASE/.venv/bin/python" manage.py test pedidos
```

No ejecutes `seed_demo`, no envíes correos y no uses la URL de Supabase para
estas pruebas.

## 4. Validar el perfil productivo y estaticos

Delega la lectura de ambos archivos a systemd; nunca ejecutes `source` ni `.`
sobre ellos. Cada unidad carga primero el `.env` base y después los overrides.
`/usr/bin/env` aplica al final SQLite, correo en memoria y scheduler apagado:

```bash
CHECK_UNIT="tcysweb-release-check-${COMMIT:0:12}"
CHECK_DB="$RELEASE/.release-check.sqlite3"
CHECK_DATABASE_URL="sqlite:///$CHECK_DB"
test ! -e "$CHECK_DB"
install -m 0600 /dev/null "$CHECK_DB"

sudo systemd-run --unit="${CHECK_UNIT}-django" --wait --pipe --collect \
  --property=Type=oneshot \
  --property=User=deploy \
  --property=Group=deploy \
  --property="WorkingDirectory=$RELEASE" \
  --property="EnvironmentFile=$BASE_ENV" \
  --property="EnvironmentFile=$PROD_OVERRIDES_ENV" \
  /usr/bin/env \
  "DATABASE_URL=$CHECK_DATABASE_URL" \
  EMAIL_BACKEND=django.core.mail.backends.locmem.EmailBackend \
  SCHEDULER_ENABLED=False \
  "$RELEASE/.venv/bin/python" manage.py check

sudo systemd-run --unit="${CHECK_UNIT}-deploy" --wait --pipe --collect \
  --property=Type=oneshot \
  --property=User=deploy \
  --property=Group=deploy \
  --property="WorkingDirectory=$RELEASE" \
  --property="EnvironmentFile=$BASE_ENV" \
  --property="EnvironmentFile=$PROD_OVERRIDES_ENV" \
  /usr/bin/env \
  "DATABASE_URL=$CHECK_DATABASE_URL" \
  EMAIL_BACKEND=django.core.mail.backends.locmem.EmailBackend \
  SCHEDULER_ENABLED=False \
  "$RELEASE/.venv/bin/python" manage.py check --deploy

sudo systemd-run --unit="${CHECK_UNIT}-static" --wait --pipe --collect \
  --property=Type=oneshot \
  --property=User=deploy \
  --property=Group=deploy \
  --property="WorkingDirectory=$RELEASE" \
  --property="EnvironmentFile=$BASE_ENV" \
  --property="EnvironmentFile=$PROD_OVERRIDES_ENV" \
  /usr/bin/env \
  "DATABASE_URL=$CHECK_DATABASE_URL" \
  EMAIL_BACKEND=django.core.mail.backends.locmem.EmailBackend \
  SCHEDULER_ENABLED=False \
  "$RELEASE/.venv/bin/python" manage.py collectstatic --noinput

rm -f -- "$CHECK_DB"
```

`check --deploy` puede advertir que HSTS vale `0`; es intencional durante el
piloto. Cualquier otro warning de seguridad se investiga antes de continuar.

Revisa si el commit contiene migraciones respecto al release activo. Durante
el bootstrap, cuando `current` aun no existe, toma el commit del checkout
productivo actual; en releases posteriores valida y usa el symlink:

```bash
if test -L "$APP_ROOT/current"; then
  CURRENT_RELEASE=$(readlink -f "$APP_ROOT/current")
  case "$CURRENT_RELEASE" in
    "$APP_ROOT"/releases/*) ;;
    *) echo 'El symlink current apunta fuera del arbol de releases' >&2; exit 1 ;;
  esac
  CURRENT_COMMIT=$(basename "$CURRENT_RELEASE")
elif test ! -e "$APP_ROOT/current"; then
  CURRENT_COMMIT=$(git -C "$ACTIVE_CHECKOUT" rev-parse HEAD)
else
  echo 'current existe pero no es un symlink' >&2
  exit 1
fi
test "$(git --git-dir="$REPO_CACHE" rev-parse "$CURRENT_COMMIT^{commit}")" = "$CURRENT_COMMIT"
git --git-dir="$REPO_CACHE" diff --name-only "$CURRENT_COMMIT" "$COMMIT" -- '*/migrations/*.py'
```

Si aparece una migracion, detente: revisa `migrate --plan`, crea y valida un
backup y solicita autorizacion antes de escribir en la base. Este release de
hardening no requiere migraciones.

## 5. Prueba paralela sin sustituir el servicio activo

Comprueba que `8012` este libre y levanta una unidad transitoria. Usa solo GET
para el smoke test; no captures ni confirmes pedidos reales.

```bash
sudo ss -lnt '( sport = :8012 )'
TEST_UNIT="tcysweb-release-test-${COMMIT:0:12}"
SMOKE_DB="/tmp/tcysweb-release-${COMMIT:0:12}.sqlite3"
SMOKE_DATABASE_URL="sqlite:///$SMOKE_DB"
test ! -e "$SMOKE_DB"
install -m 0600 /dev/null "$SMOKE_DB"

sudo systemd-run --unit="${TEST_UNIT}-migrate" --wait --pipe --collect \
  --property=Type=oneshot \
  --property=User=deploy \
  --property=Group=deploy \
  --property="WorkingDirectory=$RELEASE" \
  --property="EnvironmentFile=$BASE_ENV" \
  --property="EnvironmentFile=$PROD_OVERRIDES_ENV" \
  /usr/bin/env \
  "DATABASE_URL=$SMOKE_DATABASE_URL" \
  EMAIL_BACKEND=django.core.mail.backends.locmem.EmailBackend \
  SCHEDULER_ENABLED=False \
  "$RELEASE/.venv/bin/python" manage.py migrate --noinput

sudo systemd-run --unit="$TEST_UNIT" --collect \
  --property=User=deploy \
  --property=Group=deploy \
  --property="WorkingDirectory=$RELEASE" \
  --property="EnvironmentFile=$BASE_ENV" \
  --property="EnvironmentFile=$PROD_OVERRIDES_ENV" \
  /usr/bin/env \
  "DATABASE_URL=$SMOKE_DATABASE_URL" \
  EMAIL_BACKEND=django.core.mail.backends.locmem.EmailBackend \
  SCHEDULER_ENABLED=False \
  "$RELEASE/.venv/bin/gunicorn" proyecto.wsgi:application \
  --bind 127.0.0.1:8012 --workers 1 --timeout 60 --access-logfile - --error-logfile -

SMOKE_READY=false
SMOKE_DEADLINE=$((SECONDS + 30))
while (( SECONDS < SMOKE_DEADLINE )); do
  if sudo systemctl is-active --quiet "$TEST_UNIT" &&
     sudo ss -H -lnt 'sport = :8012' | grep -q .; then
    SMOKE_READY=true
    break
  fi
  sleep 1
done

if test "$SMOKE_READY" != true; then
  sudo journalctl -u "$TEST_UNIT" -n 100 --no-pager
  sudo systemctl stop "$TEST_UNIT" || true
  rm -f -- "$SMOKE_DB"
  echo 'La unidad transitoria no quedo lista en 30 segundos' >&2
  exit 1
fi

curl --fail --silent --show-error --output /dev/null \
  -H 'Host: tcysweb.lostocayos-lostcys.com.mx' \
  -H 'X-Forwarded-Proto: https' \
  http://127.0.0.1:8012/api/horarios/
sudo journalctl -u "$TEST_UNIT" -n 100 --no-pager
sudo systemctl stop "$TEST_UNIT"
rm -f -- "$SMOKE_DB"
```

systemd carga primero el `.env` base y después los overrides productivos. El
`/usr/bin/env` del comando aplica al final `DATABASE_URL` hacia el SQLite
temporal, por lo que ni la migracion local ni el smoke test se conectan a
Supabase. Tampoco se envían correos ni se ejecuta `seed_demo`.

Confirma que `tcysweb-prod` nunca se detuvo y que el puerto productivo conserva
su proceso. El rollback de esta prueba es simplemente detener la unidad
transitoria; conserva el release y sus logs para revision.

## Preflight Linux previo a la fase 6

La unidad canonica apunta a un solo archivo:
`/home/deploy/apps/tcysPedidosSucursales/shared/.env`. Antes de una futura
activacion autorizada, migra los dos archivos actuales a esa ubicacion
directamente en el VPS. El segundo archivo se concatena al final para conservar
la precedencia de sus overrides; ningún valor se muestra ni entra al release o
al repositorio:

```bash
TARGET_ENV="$APP_ROOT/shared/.env"
if test -e "$TARGET_ENV"; then
  validar_archivo_entorno "$TARGET_ENV"
else
  sudo install -d -o deploy -g deploy -m 0700 "$APP_ROOT/shared"
  TARGET_ENV_TMP=$(mktemp "$APP_ROOT/shared/.env.new.XXXXXX")
  trap 'rm -f -- "$TARGET_ENV_TMP"' EXIT
  chmod 600 "$TARGET_ENV_TMP"
  {
    cat "$BASE_ENV"
    printf '\n'
    cat "$PROD_OVERRIDES_ENV"
    printf '\n'
  } > "$TARGET_ENV_TMP"
  mv -T "$TARGET_ENV_TMP" "$TARGET_ENV"
  trap - EXIT
  validar_archivo_entorno "$TARGET_ENV"
fi
```

Conserva los dos archivos originales sin cambios durante toda la ventana de
rollback. No ejecutes esta migracion de entorno durante las fases 1 a 5.

Renderiza las plantillas versionadas hacia copias temporales con las rutas del
release candidato. `systemd-analyze verify` y el primer `nginx -t` no instalan
nada; el segundo `nginx -t` comprueba la configuracion activa sin recargarla:

```bash
VERIFY_DIR=$(mktemp -d /tmp/tcysweb-release-verify.XXXXXX)
SYSTEMD_VERIFY="$VERIFY_DIR/tcysweb-candidate-${COMMIT:0:12}.service"
NGINX_SITE_VERIFY="$VERIFY_DIR/tcysweb-candidate.conf"
NGINX_MAIN_VERIFY="$VERIFY_DIR/nginx.conf"
trap 'rm -f -- "$SYSTEMD_VERIFY" "$NGINX_SITE_VERIFY" "$NGINX_MAIN_VERIFY" "$VERIFY_DIR/nginx.pid"; rmdir -- "$VERIFY_DIR" 2>/dev/null || true' EXIT

sed \
  -e "s|/home/deploy/apps/tcysPedidosSucursales/current|$RELEASE|g" \
  -e "s|/home/deploy/apps/tcysPedidosSucursales/shared/.env|$TARGET_ENV|g" \
  "$RELEASE/deploy/vps/systemd/tcysweb.service" > "$SYSTEMD_VERIFY"
sudo systemd-analyze verify "$SYSTEMD_VERIFY"

sed \
  -e "s|/home/deploy/apps/tcysPedidosSucursales/current|$RELEASE|g" \
  "$RELEASE/deploy/vps/nginx/tcysweb.conf" > "$NGINX_SITE_VERIFY"
cat > "$NGINX_MAIN_VERIFY" <<EOF
error_log stderr;
pid $VERIFY_DIR/nginx.pid;
events {}
http {
    access_log off;
    include /etc/nginx/mime.types;
    include $NGINX_SITE_VERIFY;
}
EOF
sudo nginx -t -p "$VERIFY_DIR/" -c "$NGINX_MAIN_VERIFY"
sudo nginx -t

rm -f -- "$SYSTEMD_VERIFY" "$NGINX_SITE_VERIFY" "$NGINX_MAIN_VERIFY" "$VERIFY_DIR/nginx.pid"
rmdir -- "$VERIFY_DIR"
trap - EXIT
```

Inmediatamente antes de autorizar la fase 6, confirma que la produccion actual
sigue activa en `127.0.0.1:8002` y que el candidato y su entorno estan listos:

```bash
sudo systemctl is-active --quiet tcysweb-prod.service
sudo ss -H -lnt 'sport = :8002' | grep -q '127.0.0.1:8002'
test -d "$RELEASE"
test -x "$RELEASE/.venv/bin/gunicorn"
validar_archivo_entorno "$TARGET_ENV"
```

## 6. Activacion atomica (solo con autorizacion)

No ejecutes esta fase durante la prueba inicial. Antes de sustituir la unidad,
selecciona y registra uno de estos caminos de rollback:

- `bootstrap`: `current` no existe. Conserva el checkout productivo actual y
  exige que siga exactamente en `ba7330bf006417cc1377b20bdcb532a4a27c37b9`.
- `release`: `current` ya existe. Registra su destino como `previous_release`.

Ambos caminos respaldan la unidad activa antes de instalar la plantilla
canonica. Luego crean o sustituyen `current` y reinician de forma controlada:

```bash
ROLLBACK_MODE_FILE="$APP_ROOT/shared/rollback_mode"
UNIT_BACKUP="$APP_ROOT/shared/tcysweb-prod.service.before-release"

if test -L "$APP_ROOT/current"; then
  DEPLOY_MODE=release
  PREVIOUS_RELEASE=$(readlink -f "$APP_ROOT/current")
  case "$PREVIOUS_RELEASE" in
    "$APP_ROOT"/releases/*) ;;
    *) echo 'El symlink current apunta fuera del arbol de releases' >&2; exit 1 ;;
  esac
  test -x "$PREVIOUS_RELEASE/.venv/bin/gunicorn"
  printf '%s\n' "$PREVIOUS_RELEASE" > "$APP_ROOT/shared/previous_release.next"
  mv -T "$APP_ROOT/shared/previous_release.next" "$APP_ROOT/shared/previous_release"
elif test ! -e "$APP_ROOT/current"; then
  DEPLOY_MODE=bootstrap
  BOOTSTRAP_COMMIT=$(git -C "$ACTIVE_CHECKOUT" rev-parse HEAD)
  test "$BOOTSTRAP_COMMIT" = ba7330bf006417cc1377b20bdcb532a4a27c37b9
  printf '%s\n' "$ACTIVE_CHECKOUT" > "$APP_ROOT/shared/bootstrap_previous_checkout.next"
  mv -T "$APP_ROOT/shared/bootstrap_previous_checkout.next" "$APP_ROOT/shared/bootstrap_previous_checkout"
  printf '%s\n' "$BOOTSTRAP_COMMIT" > "$APP_ROOT/shared/bootstrap_previous_commit.next"
  mv -T "$APP_ROOT/shared/bootstrap_previous_commit.next" "$APP_ROOT/shared/bootstrap_previous_commit"
else
  echo 'current existe pero no es un symlink' >&2
  exit 1
fi

sudo test -f /etc/systemd/system/tcysweb-prod.service
sudo install -m 0644 /etc/systemd/system/tcysweb-prod.service "$UNIT_BACKUP"
printf '%s\n' "$DEPLOY_MODE" > "$ROLLBACK_MODE_FILE.next"
mv -T "$ROLLBACK_MODE_FILE.next" "$ROLLBACK_MODE_FILE"
sudo install -m 0644 "$RELEASE/deploy/vps/systemd/tcysweb.service" /etc/systemd/system/tcysweb-prod.service
sudo systemctl daemon-reload
ln -s "$RELEASE" "$APP_ROOT/current.next"
mv -Tf "$APP_ROOT/current.next" "$APP_ROOT/current"
sudo systemctl restart tcysweb-prod.service
sudo systemctl status tcysweb-prod.service --no-pager
curl --fail --silent --show-error --output /dev/null https://tcysweb.lostocayos-lostcys.com.mx/
```

Si el restart o smoke test falla, ejecuta inmediatamente el runbook de
rollback. Conserva Render, Supabase y al menos dos releases durante la ventana
acordada.
