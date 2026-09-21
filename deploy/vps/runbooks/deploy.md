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

La suite usa SQLite de test, backend de correo en memoria y no carga el `.env`
productivo:

```bash
cd "$RELEASE"
DJANGO_ENV=test DEBUG=True DATABASE_URL= EMAIL_BACKEND=django.core.mail.backends.locmem.EmailBackend .venv/bin/python manage.py check
DJANGO_ENV=test DEBUG=True DATABASE_URL= EMAIL_BACKEND=django.core.mail.backends.locmem.EmailBackend .venv/bin/python manage.py test pedidos
```

No ejecutes `seed_demo`, no envíes correos y no uses la URL de Supabase para
estas pruebas.

## 4. Validar el perfil productivo y estaticos

Carga ambos archivos dentro de un subshell, sin imprimirlos, y sustituye
temporalmente la base por SQLite para los checks y `collectstatic`; así se
valida el perfil real sin escribir en Supabase y las variables sensibles no
quedan exportadas en la shell del operador:

```bash
(
  set -a
  . "$BASE_ENV"
  . "$PROD_OVERRIDES_ENV"
  set +a
  DATABASE_URL="sqlite:///$RELEASE/.release-check.sqlite3" "$RELEASE/.venv/bin/python" manage.py check
  DATABASE_URL="sqlite:///$RELEASE/.release-check.sqlite3" "$RELEASE/.venv/bin/python" manage.py check --deploy
  DATABASE_URL="sqlite:///$RELEASE/.release-check.sqlite3" "$RELEASE/.venv/bin/python" manage.py collectstatic --noinput
)
```

`check --deploy` puede advertir que HSTS vale `0`; es intencional durante el
piloto. Cualquier otro warning de seguridad se investiga antes de continuar.

Revisa si el commit contiene migraciones respecto al release activo:

```bash
CURRENT_COMMIT=$(basename "$(readlink -f "$APP_ROOT/current")")
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

## 6. Activacion atomica (solo con autorizacion)

La unidad canonica apunta a un solo archivo:
`/home/deploy/apps/tcysPedidosSucursales/shared/.env`. Antes de una futura
activacion autorizada, migra los dos archivos actuales a esa ubicacion
directamente en el VPS. El segundo archivo se concatena al final para conservar
la precedencia de sus overrides; ningún valor se muestra ni entra al release o
al repositorio:

```bash
TARGET_ENV="$APP_ROOT/shared/.env"
test ! -e "$TARGET_ENV"
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
test "$(stat -c '%U:%G' "$TARGET_ENV")" = "deploy:deploy"
test "$(stat -c '%a' "$TARGET_ENV")" = "600"
```

Conserva los dos archivos originales sin cambios durante toda la ventana de
rollback. No ejecutes esta migracion de entorno durante las fases 1 a 5.

No ejecutes esta fase durante la prueba inicial. Registra la ruta y la unidad
anteriores, instala la plantilla canonica bajo el nombre activo, cambia el
symlink y reinicia de forma controlada:

```bash
readlink -f "$APP_ROOT/current" > "$APP_ROOT/shared/previous_release"
sudo install -m 0644 /etc/systemd/system/tcysweb-prod.service "$APP_ROOT/shared/tcysweb-prod.service.before-release"
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
