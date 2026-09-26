-- Compare the frozen source with the untouched restore BEFORE Django migrations.
-- Only metadata and aggregates; no business rows or secrets.
\pset format unaligned
\pset tuples_only on
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;

SELECT format(
  'SELECT %L, count(*)::bigint FROM %I.%I;',
  n.nspname || '.' || c.relname, n.nspname, c.relname
)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p')
  AND n.nspname NOT LIKE 'pg_%'
  AND n.nspname <> 'information_schema'
ORDER BY n.nspname, c.relname
\gexec

SELECT 'pedido_total', coalesce(sum(total), 0), count(DISTINCT codigo_publico)
FROM pedidos_pedido;
SELECT 'item_subtotal', coalesce(sum(subtotal), 0)
FROM pedidos_itempedido;
SELECT 'macropedido_total', coalesce(sum(total), 0), count(DISTINCT codigo_publico)
FROM pedidos_macropedido;
SELECT 'migration', app, name FROM django_migrations ORDER BY app, name;
SELECT 'constraint', n.nspname, c.relname, con.conname, con.contype,
       con.convalidated, pg_get_constraintdef(con.oid)
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
ORDER BY n.nspname, c.relname, con.conname;
SELECT 'index', n.nspname, t.relname, i.relname,
       x.indisprimary, x.indisunique, x.indisvalid, x.indisready, x.indislive,
       pg_get_indexdef(i.oid)
FROM pg_index x
JOIN pg_class t ON t.oid = x.indrelid
JOIN pg_class i ON i.oid = x.indexrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
ORDER BY n.nspname, t.relname, i.relname;
SELECT 'sequence', schemaname, sequencename, start_value, increment_by,
       min_value, max_value, cycle, last_value
FROM pg_sequences
WHERE schemaname NOT LIKE 'pg_%' AND schemaname <> 'information_schema'
ORDER BY schemaname, sequencename;

COMMIT;
