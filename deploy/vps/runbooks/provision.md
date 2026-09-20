# Aprovisionamiento inicial

Este procedimiento prepara la estructura de releases. No sustituye ni reinicia
`tcysweb-prod`, no cambia DNS y no toca Supabase.

## 1. Comprobaciones

Como operador con `sudo`, registra la version de Ubuntu, CPU, memoria, disco,
hora/NTP, servicios y puertos. Verifica que UFW publique solo SSH controlado,
HTTP y HTTPS. PostgreSQL debe seguir ligado a loopback; nunca abras `5432`.

```bash
lsb_release -a
nproc
free -h
df -h
timedatectl
sudo ss -lntup
sudo ufw status verbose
```

## 2. Paquetes y Python

Instala solo los paquetes de sistema requeridos por Git, Nginx, compilacion y
libpq. Usa el runtime administrado ya validado en el VPS; no agregues un PPA.

```bash
sudo apt-get update
sudo apt-get install --yes git nginx build-essential libpq-dev ca-certificates
uv python install 3.13.12
/home/deploy/.local/share/uv/python/cpython-3.13.12-linux-x86_64-gnu/bin/python3.13 --version
```

El ultimo comando debe informar exactamente `Python 3.13.12`.

## 3. Directorios y secretos

```bash
sudo install -d -o deploy -g deploy -m 0750 /home/deploy/apps/tcysPedidosSucursales
sudo -u deploy install -d -m 0750 /home/deploy/apps/tcysPedidosSucursales/releases
sudo -u deploy install -d -m 0700 /home/deploy/apps/tcysPedidosSucursales/shared
sudo -u deploy install -m 0600 deploy/vps/env.example /home/deploy/apps/tcysPedidosSucursales/shared/.env
```

Edita ese archivo directamente en el VPS. Genera una `SECRET_KEY` nueva con
`django.core.management.utils.get_random_secret_key`; no reutilices valores de
conversaciones, no los pegues en Git y no los muestres en logs. Conserva
`DATABASE_URL` apuntando a Supabase durante esta fase.

Comprueba solo nombres y permisos, nunca el contenido:

```bash
stat -c '%a %U %G %n' /home/deploy/apps/tcysPedidosSucursales/shared/.env
```

El resultado esperado es `600 deploy deploy .../.env`.

## 4. Cache Git

```bash
sudo -u deploy git clone --mirror https://github.com/ivanbn05-create/tcysPedidosSucursales /home/deploy/apps/tcysPedidosSucursales/repo.git
```

El cache sirve para obtener commits y producir archivos con `git archive`; no
es el checkout productivo y nunca se usa `git pull`.

## 5. Plantillas de servicio y proxy

Revisa el diff contra las unidades activas antes de copiar nada. La plantilla
canonica usa `127.0.0.1:8002`; Gunicorn no debe escuchar en una interfaz
publica.

```bash
systemd-analyze verify "$(pwd)/deploy/vps/systemd/tcysweb.service"
sudo install -m 0644 deploy/vps/nginx/tcysweb.conf /etc/nginx/sites-available/tcysweb
sudo nginx -t
```

No copies ni habilites todavia la unidad nueva: `tcysweb-prod.service` sigue
siendo la unidad activa y ya ocupa el puerto `8002`. Cuando DNS resuelva al VPS,
Certbot o el gestor TLS elegido debe emitir el certificado y modificar Nginx;
despues ejecuta `sudo nginx -t` antes de cualquier reload. Las llaves privadas
permanecen fuera del repositorio.
