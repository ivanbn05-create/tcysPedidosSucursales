# Credenciales POS v2 por Edge y sucursal

Esta revisión es candidata y privada. La migración `pedidos.0016` crea `PosApiCredential`; no se aplica a Pedidos productivo hasta un corte autorizado. `GET /api/v1/pos/pedidos/`, `POS_API_TOKENS` y `POS_API_ALLOWED_SUCURSAL_IDS` conservan su semántica anterior. V2 sólo acepta una credencial propia con `orders:v2:read`, UUID de instalación Edge y un `SucursalCliente.id` de tipo `sucursal` explícitamente aprobado. La tabla sólo guarda SHA-256 de un bearer aleatorio de 48 bytes, no el bearer.

Antes de emitir una credencial, aprobar la matriz `Sucursal POS UUID/clave` ↔ `Edge UUID` ↔ `SucursalCliente.id` ↔ `Branch central`. No asociar por nombre. Una sucursal sin correspondencia permanece pendiente; el comando no deriva ninguna identidad del nombre.

En Linux, con el entorno de laboratorio y una carpeta fuera del release/repo, privada `0700` y propiedad del operador:

```bash
python manage.py issue_pos_v2_credential \
  --edge-id '<EDGE_UUID_APROBADO>' --sucursal-id '<ID_PEDIDOS_APROBADO>' \
  --output '/home/deploy/secrets-lab/pedidos-edge-01.token' \
  --reference 'LAB-CAMBIO-001' --expires-days 30 --confirm
```

El comando crea exclusivamente un archivo nuevo `0600` con el bearer, lo imprime sólo como ID de credencial en stdout y falla si la ruta está dentro de un release, un repositorio o un directorio público. El archivo se entrega por canal privado y se elimina después de instalar el secreto como `PEDIDOS_API_TOKEN` en el Edge de laboratorio. No incluirlo en logs, tickets, respaldos de código ni argumentos de procesos.

Rotación inmediata, con el mismo Edge y sucursal:

```bash
python manage.py issue_pos_v2_credential \
  --edge-id '<EDGE_UUID_APROBADO>' --sucursal-id '<ID_PEDIDOS_APROBADO>' \
  --rotate-from '<CREDENTIAL_ID_ANTERIOR>' \
  --output '/home/deploy/secrets-lab/pedidos-edge-01-nuevo.token' \
  --reference 'LAB-CAMBIO-002' --confirm
```

La nueva credencial se crea y la anterior se revoca en una transacción. Para revocación sin reemplazo:

```bash
python manage.py revoke_pos_v2_credential \
  --credential-id '<CREDENTIAL_ID>' --reference 'LAB-CAMBIO-003' --confirm
```

Un bearer inválido, expirado o revocado recibe 401. Una credencial válida sin scope o que pide una sucursal ajena recibe 403. El rate limit de aplicación sigue usando `LocMemCache` por worker; antes de publicar la ruta se requiere límite perimetral compartido y validación del proxy TLS. Los campos permanentes `credential_id`, `edge_id`, `sucursal_cliente_id`, hash, scope, creación, expiración, revocación y referencias de cambio deben incluirse en el respaldo/restauración de la base de Pedidos; no copiar el archivo de entrega de bearer al backup de datos.

La rama candidata conserva Supabase legacy y v1. Ninguna credencial v2 se emite para una sucursal real ni se cambia 8002 hasta aprobar matriz, pruebas PostgreSQL/E2E y plan de corte.
