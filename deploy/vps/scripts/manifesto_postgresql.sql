-- Metadatos de comparación para Pedidos. Sólo SELECT, sin filas de negocio.
-- Ejecutar con psql -X -v ON_ERROR_STOP=1 -f este_archivo sobre cada base.
-- En origen activo, este manifiesto NO comparte por sí solo el snapshot de
-- pg_dump; para igualdad exacta congelar escritores o coordinar un snapshot.

BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;

SELECT current_database() AS base, current_setting('server_version') AS version,
       current_setting('TimeZone') AS zona_horaria,
       current_setting('lc_collate') AS collation;

SELECT app, name FROM django_migrations ORDER BY app, name;

SELECT extname, extversion FROM pg_extension ORDER BY extname;

-- \gexec ejecuta sólo SELECT generados desde identificadores escapados. Un
-- error de permiso en una tabla detiene psql cuando ON_ERROR_STOP=1.
SELECT format(
  'SELECT %L AS tabla, count(*)::bigint AS filas FROM %I.%I;',
  n.nspname || '.' || c.relname, n.nspname, c.relname
)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p')
  AND n.nspname NOT LIKE 'pg_%'
  AND n.nspname <> 'information_schema'
ORDER BY n.nspname, c.relname
\gexec

-- Min/max de todas las tablas con una columna id numérica.
SELECT format(
  'SELECT %L AS tabla, min(id) AS id_min, max(id) AS id_max FROM %I.%I;',
  n.nspname || '.' || c.relname, n.nspname, c.relname
)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'id'
WHERE c.relkind IN ('r', 'p')
  AND n.nspname NOT LIKE 'pg_%'
  AND n.nspname <> 'information_schema'
  AND NOT a.attisdropped
  AND a.atttypid IN ('smallint'::regtype, 'integer'::regtype, 'bigint'::regtype)
ORDER BY n.nspname, c.relname
\gexec

SELECT 'pedidos_pedido' AS tabla, count(*) AS filas,
       min(id) AS id_min, max(id) AS id_max,
       coalesce(sum(total), 0) AS suma_total,
       count(DISTINCT codigo_publico) AS codigos_publicos_unicos
FROM pedidos_pedido;

SELECT 'pedidos_itempedido' AS tabla, count(*) AS filas,
       min(id) AS id_min, max(id) AS id_max,
       coalesce(sum(subtotal), 0) AS suma_subtotal
FROM pedidos_itempedido;

SELECT 'pedidos_macropedido' AS tabla, count(*) AS filas,
       min(id) AS id_min, max(id) AS id_max,
       coalesce(sum(total), 0) AS suma_total,
       count(DISTINCT codigo_publico) AS codigos_publicos_unicos
FROM pedidos_macropedido;

SELECT sucursal_cliente_id, count(*) AS pedidos_confirmados
FROM pedidos_pedido
WHERE estado = 'confirmado' AND NOT eliminado
GROUP BY sucursal_cliente_id
ORDER BY sucursal_cliente_id;

SELECT n.nspname AS esquema, c.relname AS tabla, con.conname AS fk,
       con.convalidated AS validada,
       pg_get_constraintdef(con.oid) AS definicion
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE con.contype = 'f'
  AND n.nspname NOT LIKE 'pg_%'
  AND n.nspname <> 'information_schema'
ORDER BY n.nspname, c.relname, con.conname;

-- last_value puede ser NULL si el rol carece de permiso: eso deja incompleta
-- la verificación y exige repetirla con un rol autorizado, sin usar nextval.
SELECT schemaname, sequencename, start_value, increment_by,
       min_value, max_value, cycle, last_value
FROM pg_sequences
WHERE schemaname NOT LIKE 'pg_%'
  AND schemaname <> 'information_schema'
ORDER BY schemaname, sequencename;

COMMIT;
