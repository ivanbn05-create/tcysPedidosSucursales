#!/usr/bin/env bash
# Copia de ensayo externa -> base PostgreSQL local VACÍA y aislada.
# Nunca crea/limpia bases, no modifica origen y no ejecuta migraciones Django.
set +x
set -euo pipefail
umask 077

usage() {
  printf '%s\n' 'Uso: ensayar_copia_postgresql.sh --source-service NOMBRE --target-service NOMBRE --output-dir /home/deploy/migration-private/ENSAYO' >&2
  exit 64
}

fail() {
  printf 'Ensayo detenido: %s\n' "$1" >&2
  exit 1
}

(( $# == 6 )) || usage
source_service=
target_service=
output_dir=
while (( $# > 0 )); do
  case "$1" in
    --source-service) source_service=${2-}; shift 2 ;;
    --target-service) target_service=${2-}; shift 2 ;;
    --output-dir) output_dir=${2-}; shift 2 ;;
    *) usage ;;
  esac
done

[[ "$source_service" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || usage
[[ "$target_service" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || usage
[[ "$source_service" != "$target_service" ]] || fail 'Los servicios de origen y destino son iguales.'
[[ "$target_service" =~ (ensayo|staging|test) ]] || fail 'El servicio de destino debe identificarse como ensayo/staging/test.'

for command_name in psql pg_dump pg_restore sha256sum stat realpath find diff id; do
  command -v "$command_name" >/dev/null 2>&1 || fail "Falta la herramienta $command_name."
done

operator_uid=$(id -u)
for secret_path in "${PGSERVICEFILE:-}" "${PGPASSFILE:-}"; do
  [[ "$secret_path" == /* && -f "$secret_path" && ! -L "$secret_path" && -r "$secret_path" ]] ||
    fail 'Falta un PGSERVICEFILE/PGPASSFILE regular, absoluto y legible.'
  [[ $(stat -c '%a' -- "$secret_path") == 600 ]] || fail 'Un archivo de conexión no tiene modo 0600.'
  [[ $(stat -c '%u' -- "$secret_path") == "$operator_uid" ]] || fail 'Un archivo de conexión no pertenece al operador.'
done

# Libpq lee únicamente los servicios y passfile privados indicados. Una
# variable PG* heredada no puede reemplazar host/base/usuario del servicio.
unset PGHOST PGHOSTADDR PGPORT PGDATABASE PGUSER PGPASSWORD PGSSLMODE
unset PGSSLROOTCERT PGOPTIONS PGSERVICE PGCHANNELBINDING
export PGCONNECT_TIMEOUT=10

[[ "$output_dir" == /home/deploy/migration-private/* ]] ||
  fail 'El directorio de salida debe estar bajo /home/deploy/migration-private/.'
[[ -d "$output_dir" && ! -L "$output_dir" && -w "$output_dir" ]] ||
  fail 'El directorio de salida privado debe existir y ser escribible.'
[[ $(realpath -e -- "$output_dir") == "$output_dir" ]] || fail 'La ruta de salida debe ser canónica.'
[[ $(stat -c '%a' -- "$output_dir") == 700 ]] || fail 'El directorio de salida requiere modo 0700.'
[[ $(stat -c '%u' -- "$output_dir") == "$operator_uid" ]] || fail 'El directorio de salida no pertenece al operador.'
[[ -z $(find "$output_dir" -mindepth 1 -maxdepth 1 -print -quit) ]] ||
  fail 'El directorio de salida debe estar vacío; no se sobrescribe un ensayo previo.'

source_conn="service=$source_service"
target_conn="service=$target_service"
psql_readonly() {
  psql --no-psqlrc --no-password --quiet --tuples-only --no-align \
    --set=ON_ERROR_STOP=1 --dbname="$1" --command="$2"
}

source_database=$(psql_readonly "$source_conn" 'SELECT current_database();')
target_database=$(psql_readonly "$target_conn" 'SELECT current_database();')
[[ "$target_database" =~ (ensayo|staging|test) ]] ||
  fail 'La base real del destino debe identificarse como ensayo/staging/test.'

source_address=$(psql_readonly "$source_conn" "SELECT coalesce(inet_server_addr()::text, 'socket') || ':' || coalesce(inet_server_port()::text, 'socket') || ':' || current_database();")
target_address=$(psql_readonly "$target_conn" "SELECT coalesce(inet_server_addr()::text, 'socket') || ':' || coalesce(inet_server_port()::text, 'socket') || ':' || current_database();")
[[ "$source_address" != "$target_address" ]] || fail 'Origen y destino resuelven a la misma conexión.'
target_host=$(psql_readonly "$target_conn" "SELECT coalesce(inet_server_addr()::text, 'socket');")
[[ "$target_host" == 127.0.0.1 || "$target_host" == ::1 || "$target_host" == socket ]] ||
  fail 'El destino PostgreSQL no escucha en loopback/socket.'

source_migrations=$(psql_readonly "$source_conn" 'SELECT count(*) FROM django_migrations;')
[[ "$source_migrations" =~ ^[1-9][0-9]*$ ]] || fail 'El origen no parece una base Django migrada.'

target_objects_sql="SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','p','v','m','S','f') AND n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema';"
target_objects=$(psql_readonly "$target_conn" "$target_objects_sql")
[[ "$target_objects" == 0 ]] || fail 'La base de ensayo no está vacía.'
target_clients=$(psql_readonly "$target_conn" "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND backend_type='client backend' AND pid<>pg_backend_pid();")
[[ "$target_clients" == 0 ]] || fail 'Hay otras conexiones cliente en la base de ensayo.'

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
manifest_sql="$script_dir/manifesto_postgresql.sql"
[[ -f "$manifest_sql" ]] || fail 'Falta el manifiesto SQL versionado.'

archive="$output_dir/pedidos-ensayo.dump"
checksum="$output_dir/pedidos-ensayo.dump.sha256"
target_manifest="$output_dir/destino-manifiesto.txt"
source_schema="$output_dir/origen-esquema.txt"
target_schema="$output_dir/destino-esquema.txt"
report="$output_dir/resultado.txt"

schema_sql="SELECT n.nspname || '.' || c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','p','v','m','S','f') AND n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema' ORDER BY 1;"
psql_readonly "$source_conn" "$schema_sql" > "$source_schema"

dump_start=$SECONDS
pg_dump --no-password --dbname="$source_conn" --format=custom \
  --no-owner --no-acl --file="$archive"
dump_seconds=$((SECONDS - dump_start))
sha256sum "$archive" > "$checksum"
sha256sum --check --status "$checksum" || fail 'Falló la verificación SHA-256 del dump.'
pg_restore --list "$archive" >/dev/null || fail 'El archivo no es un dump custom legible.'

# Segunda comprobación antes de la primera escritura. No hay --clean ni DROP.
target_objects=$(psql_readonly "$target_conn" "$target_objects_sql")
[[ "$target_objects" == 0 ]] || fail 'El destino dejó de estar vacío antes del restore.'
target_clients=$(psql_readonly "$target_conn" "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND backend_type='client backend' AND pid<>pg_backend_pid();")
[[ "$target_clients" == 0 ]] || fail 'Apareció otra conexión al destino antes del restore.'

restore_start=$SECONDS
pg_restore --no-password --dbname="$target_conn" --no-owner --no-acl \
  --single-transaction --exit-on-error "$archive"
restore_seconds=$((SECONDS - restore_start))

psql --no-psqlrc --no-password --set=ON_ERROR_STOP=1 \
  --dbname="$target_conn" --file="$manifest_sql" > "$target_manifest"
psql_readonly "$target_conn" "$schema_sql" > "$target_schema"
diff --brief -- "$source_schema" "$target_schema" >/dev/null ||
  fail 'El esquema de tablas/secuencias del destino difiere del origen.'

printf 'estado=restore_completo\norigen_servicio=%s\ndestino_servicio=%s\norigen_base=%s\ndestino_base=%s\ndump_segundos=%s\nrestore_segundos=%s\n' \
  "$source_service" "$target_service" "$source_database" "$target_database" \
  "$dump_seconds" "$restore_seconds" > "$report"
printf '%s\n' 'Ensayo completado. Revisar manifiesto, FK, secuencias, permisos y pruebas funcionales antes de cualquier corte.'
