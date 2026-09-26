-- Fail if any serial/identity sequence would issue an ID already in its table.
-- No nextval/setval and no business rows are printed.
\pset format unaligned
\pset tuples_only on
SELECT format(
  'SELECT 1 / CASE WHEN s.last_value > t.max_id OR (s.last_value = t.max_id AND s.is_called) THEN 1 ELSE 0 END FROM %I.%I AS s CROSS JOIN (SELECT coalesce(max(%I), 0) AS max_id FROM %I.%I) AS t;',
  seq_ns.nspname, seq.relname, att.attname, table_ns.nspname, tbl.relname
)
FROM pg_class seq
JOIN pg_namespace seq_ns ON seq_ns.oid = seq.relnamespace
JOIN pg_depend dep ON dep.objid = seq.oid
  AND dep.classid = 'pg_class'::regclass
  AND dep.refclassid = 'pg_class'::regclass
  AND dep.deptype IN ('a', 'i')
JOIN pg_class tbl ON tbl.oid = dep.refobjid
JOIN pg_namespace table_ns ON table_ns.oid = tbl.relnamespace
JOIN pg_attribute att ON att.attrelid = tbl.oid AND att.attnum = dep.refobjsubid
WHERE seq.relkind = 'S'
  AND seq_ns.nspname NOT LIKE 'pg_%'
  AND table_ns.nspname NOT LIKE 'pg_%'
ORDER BY seq_ns.nspname, seq.relname
\gexec
