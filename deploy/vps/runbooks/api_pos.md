# API POS: staging, release y rollback

Este procedimiento no abre PostgreSQL, no cambia DNS y no sustituye
`DATABASE_URL` productivo. Reutiliza el esquema de releases de `deploy.md` y la
API siempre consulta la base configurada por el proceso Django que la sirve.

## 1. Secretos y configuración

Genera un token aleatorio de al menos 32 caracteres fuera de Git. Define en el
entorno productivo, sin imprimir valores:

```text
POS_API_TOKENS=<TOKEN_NUEVO>
POS_API_ALLOWED_SUCURSAL_IDS=<IDS_CONFIRMADOS_SEPARADOS_POR_COMA>
POS_API_DEFAULT_PAGE_SIZE=100
POS_API_MAX_PAGE_SIZE=500
POS_API_MAX_WINDOW_DAYS=31
POS_API_RATE_LIMIT_PER_MINUTE=120
POS_API_REQUIRE_HTTPS=True
```

Confirma los IDs contra los registros reales de `pedidos_sucursalcliente` y la
configuración del POS. No uses un rango supuesto. Conserva los secretos en
`/home/deploy/apps/tcysPedidosSucursales/shared/.env` con modo `600`, nunca en
el release o el repositorio.

Para staging con la copia PostgreSQL local crea, directamente en el VPS:

```text
/home/deploy/secrets/tcysweb-pos-staging.env
```

Debe tener propietario/grupo `deploy:deploy`, modo `600` y contener el
`DATABASE_URL` de la copia local, un token de staging, la allowlist y
`POS_API_REQUIRE_HTTPS=False`. Este último valor sólo se admite porque la unidad
de staging escucha exclusivamente en loopback. Genera tokens URL-safe (por
ejemplo, 32 bytes aleatorios codificados en hexadecimal), sin comas ni saltos
de línea.

## 2. Construir y validar el release

Ejecuta las fases 1 a 4 de `deploy.md` con el SHA revisado. Además:

```bash
"$RELEASE/.venv/bin/python" manage.py test pedidos.test_api_pos
"$RELEASE/.venv/bin/python" manage.py makemigrations --check --dry-run
```

La API no agrega migraciones. Si aparece una, detente y revisa el diff antes de
continuar.

## 3. Staging contra PostgreSQL local

Valida el entorno productivo consolidado y el archivo de staging sin
mostrarlos:

```bash
PRODUCTION_ENV="$APP_ROOT/shared/.env"
STAGING_ENV=/home/deploy/secrets/tcysweb-pos-staging.env
for archivo in "$PRODUCTION_ENV" "$STAGING_ENV"; do
  test -f "$archivo"
  test -r "$archivo"
  test "$(stat -c '%U:%G' "$archivo")" = deploy:deploy
  test "$(stat -c '%a' "$archivo")" = 600
done

if ! LISTEN_8012=$(sudo ss -H -lnt 'sport = :8012'); then
  echo 'No se pudo comprobar el puerto de staging 8012.' >&2
  exit 1
fi
if test -n "$LISTEN_8012"; then
  echo 'El puerto de staging 8012 ya esta en uso.' >&2
  exit 1
fi
unset LISTEN_8012
```

Levanta una unidad transitoria. El archivo de staging se carga después del
entorno productivo y sobrescribe `DATABASE_URL`, token, allowlist y requisito
HTTPS sin hacer `source` desde shell:

```bash
STAGING_UNIT="tcysweb-pos-staging-${COMMIT:0:12}"
sudo systemd-run --unit="$STAGING_UNIT" --collect \
  --property=User=deploy \
  --property=Group=deploy \
  --property="WorkingDirectory=$RELEASE" \
  --property="EnvironmentFile=$PRODUCTION_ENV" \
  --property="EnvironmentFile=$STAGING_ENV" \
  /usr/bin/env \
  EMAIL_BACKEND=django.core.mail.backends.locmem.EmailBackend \
  SCHEDULER_ENABLED=False \
  "$RELEASE/.venv/bin/gunicorn" proyecto.wsgi:application \
  --bind 127.0.0.1:8012 --workers 1 --timeout 60 --access-logfile - --error-logfile -

STAGING_READY=false
STAGING_DEADLINE=$((SECONDS + 30))
while (( SECONDS < STAGING_DEADLINE )); do
  if sudo systemctl is-active --quiet "$STAGING_UNIT" &&
     sudo ss -H -lnt 'sport = :8012' | grep -q .; then
    STAGING_READY=true
    break
  fi
  sleep 1
done

if test "$STAGING_READY" != true; then
  sudo journalctl -u "$STAGING_UNIT" -n 100 --no-pager
  sudo systemctl stop "$STAGING_UNIT" || true
  echo 'La API POS de staging no quedo lista en 30 segundos.' >&2
  exit 1
fi
```

Endpoint de staging, accesible sólo desde el VPS:

```text
http://127.0.0.1:8012/api/v1/pos/pedidos/
```

Introduce el token de staging sin eco ni historial y pásalo a curl por stdin,
no como argumento visible del proceso:

```bash
POS_SMOKE_JSON=$(mktemp /tmp/tcys-pos-smoke.XXXXXX.json)
limpiar_pos_staging() {
  unset POS_API_SMOKE_TOKEN
  rm -f -- "$POS_SMOKE_JSON"
  sudo systemctl stop "$STAGING_UNIT" >/dev/null 2>&1 || true
}
trap limpiar_pos_staging EXIT

read -rsp 'Token POS staging: ' POS_API_SMOKE_TOKEN
printf '\n'
DESDE=$(date -u -d '1 hour ago' '+%Y-%m-%dT%H:%M:%SZ')
HASTA=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
if ! printf 'header = "Authorization: Bearer %s"\n' "$POS_API_SMOKE_TOKEN" | \
  curl --config - --fail --silent --show-error \
    --get --data-urlencode "desde=$DESDE" --data-urlencode "hasta=$HASTA" \
    --data-urlencode 'limite=1' \
    -H 'Host: tcysweb.lostocayos-lostcys.com.mx' \
    http://127.0.0.1:8012/api/v1/pos/pedidos/ > "$POS_SMOKE_JSON"; then
  sudo journalctl -u "$STAGING_UNIT" -n 100 --no-pager
  exit 1
fi
unset POS_API_SMOKE_TOKEN
if ! "$RELEASE/.venv/bin/python" -m json.tool "$POS_SMOKE_JSON" > /dev/null; then
  sudo journalctl -u "$STAGING_UNIT" -n 100 --no-pager
  exit 1
fi
rm -f -- "$POS_SMOKE_JSON"
sudo journalctl -u "$STAGING_UNIT" -n 100 --no-pager
sudo systemctl stop "$STAGING_UNIT"
trap - EXIT
```

No imprimas el JSON contra una copia con datos reales. Al terminar:

```bash
sudo systemctl is-active --quiet tcysweb-prod.service
sudo ss -H -lnt 'sport = :8002' | grep -q '127.0.0.1:8002'
```

## 4. Release productivo previsto

No actives hasta que staging y el agente POS pasen el flujo completo. Ejecuta
el preflight Linux y la fase 6 de `deploy.md`; no cambies `DATABASE_URL`. El
endpoint previsto es:

```text
https://tcysweb.lostocayos-lostcys.com.mx/api/v1/pos/pedidos/
```

Prueba autenticación, una ventana pequeña y paginación. No registres tokens ni
payloads reales. La unidad y Nginx existentes ya aplican proxy HTTPS y timeout
de 60 segundos.

## 5. Rollback

La API no tiene migraciones. Si falla, ejecuta `rollback.md` para restaurar el
release y la unidad anteriores. El agente POS debe conservar su cursor y
reintentar después; no se cambia ni se restaura la base. Los secretos nuevos
pueden permanecer durante el diagnóstico porque el release anterior no los
consume; retíralos posteriormente de forma controlada.

## 6. Checklist antes de retirar acceso directo a Supabase

- El POS almacena `pedido.id` e `item.id` remotos con restricciones únicas.
- Repetir una página no crea ventas, pedidos ni renglones duplicados.
- El cursor se confirma sólo después de una transacción local completa.
- Empates de `fecha_confirmacion` continúan correctamente por `pedido.id`.
- Los decimales se procesan como decimales, no como `float` binario.
- Se probaron pérdida de Internet, timeout, 429 y reinicio del VPS.
- Se validaron todos los IDs de sucursal configurados.
- El token puede rotarse sin interrupción y nunca aparece en logs.
- La integración usa sólo HTTPS en producción.
- El POS ya no necesita credenciales PostgreSQL.
- Existe observación y rollback de al menos una ventana operativa completa.
- Supabase permanece disponible hasta una autorización de corte separada.
