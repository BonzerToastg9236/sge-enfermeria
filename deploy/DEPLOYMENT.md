# Guía de Despliegue — Sistema de Gestión Escolar (SGE)

Esta guía asume un VPS con **Ubuntu 24.04** (o 22.04, es casi idéntico) y que
ya tienes acceso por SSH como root o con un usuario con `sudo`.

Reemplaza `TU_DOMINIO.com` y `tu_usuario_github` por los tuyos reales en
todos los comandos.

> **¿Servidor en una casa u oficina (no un VPS)?** Los pasos 1-8, 11 y 12 son
> iguales. Lee primero el **Anexo A** al final: explica cómo exponerlo a
> internet (IP pública o túnel), la energía y los respaldos fuera de casa.

---

## 0. Elegir y contratar el VPS

Cualquier proveedor sirve (DigitalOcean, Linode, Vultr, Hostinger, un VPS
mexicano local, etc.). Para este proyecto, un plan económico alcanza de
sobra al inicio:
- **1 vCPU, 1-2 GB RAM, 25 GB de disco** es suficiente para cientos de
  alumnos y uso normal de Control Escolar. Si crece mucho el volumen de
  documentos subidos, lo único que probablemente necesites ampliar después
  es el disco, no el CPU/RAM.
- Elige **Ubuntu 24.04 LTS** como sistema operativo al crear el servidor.

---

## 1. Configuración inicial del servidor (seguridad básica)

Conéctate por SSH y crea un usuario dedicado — **nunca corras la app como
root**:

```bash
ssh root@IP_DE_TU_VPS

adduser sge
usermod -aG sudo sge
su - sge
```

**Firewall básico** (deja pasar solo SSH, HTTP y HTTPS):

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
```

---

## 2. Instalar todo lo necesario

```bash
sudo apt update && sudo apt upgrade -y

sudo apt install -y python3 python3-venv python3-pip git \
    postgresql postgresql-contrib nginx redis-server
```

---

## 3. Base de datos PostgreSQL

```bash
sudo -u postgres psql
```

Dentro de la consola de PostgreSQL:

```sql
CREATE DATABASE sge_produccion;
CREATE USER sge_user WITH PASSWORD 'ELIGE_UNA_CONTRASEÑA_FUERTE_AQUI';
GRANT ALL PRIVILEGES ON DATABASE sge_produccion TO sge_user;

-- IMPORTANTE (PostgreSQL 15 y superior, que es lo que trae Ubuntu 24.04
-- de fábrica): desde la versión 15, el owner de una base de datos YA NO
-- tiene automáticamente permiso para crear tablas dentro del esquema
-- "public". Sin este GRANT adicional, "flask db upgrade" en el paso 6
-- falla con el error "permission denied for schema public".
\c sge_produccion
GRANT ALL ON SCHEMA public TO sge_user;
ALTER DATABASE sge_produccion OWNER TO sge_user;
\q
```

Guarda esa contraseña — la vas a necesitar en el `.env` del paso 5.

> ⚠️ **Sobre la contraseña que elijas (D5):** usa solo letras y números.
> El archivo `.env` se carga en producción vía `EnvironmentFile=` en
> `sge.service`, y **systemd no interpreta `.env` igual que una terminal
> bash** — no procesa comillas, y trata `#` como inicio de un comentario
> (cortando todo lo que sigue). Si tu contraseña incluye `#`, `@`, `/`,
> `:` u otros caracteres especiales, `DATABASE_URL` puede llegar truncada
> a Gunicorn y la app fallará al conectar con un error confuso que no
> menciona la contraseña para nada. Genera una así de fácil:
>
> ```bash
> python3 -c "import secrets; print(secrets.token_urlsafe(24))"
> ```
>
> Esto genera solo letras, números, `-` y `_` — segura y sin riesgo de
> romper `EnvironmentFile`. Si prefieres usar caracteres especiales de
> todos modos, tendrás que URL-encodearlos dentro de `DATABASE_URL`
> (por ejemplo `@` se escribe `%40`), pero es innecesariamente frágil;
> mejor evitarlo.

---

## 4. Clonar el proyecto

```bash
cd ~
git clone https://github.com/tu_usuario_github/sge-enfermeria.git sge_enfermeria
cd sge_enfermeria

python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt -r requirements-prod.txt
```

---

## 5. Variables de entorno de producción

```bash
cp .env.example .env
nano .env
```

Déjalo así (con tus datos reales):

```
SECRET_KEY=genera-una-clave-larga-y-aleatoria-aqui-nunca-uses-la-de-desarrollo
FLASK_ENV=production
DATABASE_URL=postgresql://sge_user:ELIGE_UNA_CONTRASEÑA_FUERTE_AQUI@localhost:5432/sge_produccion
RATELIMIT_STORAGE_URI=redis://localhost:6379
```

Para generar un `SECRET_KEY` fuerte de una línea:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

---

## 6. Crear las tablas (con Flask-Migrate, sin perder nada a futuro)

```bash
export FLASK_APP=app.py
export FLASK_ENV=production

flask db upgrade
python seed.py --produccion
python crear_admin.py
```

**Usa `--produccion` (no `python seed.py` a secas):** el modo demo crea planes de
ingeniería con materias de ejemplo que los aspirantes verían en `/registro`. El
modo de producción crea solo el plan de **Licenciatura en Enfermería** (sin
materias de ejemplo), el catálogo de conceptos de cobro (con "Colegiatura" ya
marcada como mensualidad) y la configuración neutra de recargos. Al terminar
imprime los pasos que siguen desde la pantalla (precios, recargos, materias del
plan oficial).

`flask db upgrade` debe terminar sin errores y aplicar 8 migraciones; se probó
desde cero contra PostgreSQL 16 (2026-09-20). `crear_admin.py` rechaza claves de
menos de 10 caracteres o de las más comunes.

`crear_admin.py` te va a pedir usuario y contraseña por consola — esa es tu
cuenta real de Directivo en producción (usa una contraseña distinta a la de
tu entorno de desarrollo local).

---

## 7. Carpeta de logs (para Gunicorn)

```bash
mkdir -p ~/sge_enfermeria/logs
```

---

## 8. Gunicorn como servicio (systemd)

Copia el archivo de servicio que ya está en tu proyecto:

```bash
sudo cp ~/sge_enfermeria/deploy/sge.service /etc/systemd/system/sge.service
```

Ábrelo y confirma que las rutas coincidan con tu usuario/ubicación real
(`/home/sge/sge_enfermeria/...`) si usaste un usuario distinto a `sge`:

```bash
sudo nano /etc/systemd/system/sge.service
```

Actívalo:

```bash
sudo systemctl daemon-reload
sudo systemctl start sge
sudo systemctl enable sge
sudo systemctl status sge
```

Si `status` muestra `active (running)` en verde, Gunicorn ya está sirviendo
tu app internamente (todavía no accesible desde internet — falta Nginx).

Si algo falla, revisa el detalle con:

```bash
sudo journalctl -u sge -n 50 --no-pager
```

---

## 9. Nginx como proxy inverso

```bash
sudo cp ~/sge_enfermeria/deploy/nginx_sge.conf /etc/nginx/sites-available/sge
sudo nano /etc/nginx/sites-available/sge
```

Cambia `server_name TU_DOMINIO.com www.TU_DOMINIO.com;` por tu dominio real
(o la IP del VPS si aún no tienes dominio).

```bash
sudo ln -s /etc/nginx/sites-available/sge /etc/nginx/sites-enabled/
sudo rm /etc/nginx/sites-enabled/default   # quita la página de bienvenida default de Nginx
sudo nginx -t                              # valida que la configuración esté bien escrita
sudo systemctl restart nginx
```

En este punto, entrando a `http://TU_DOMINIO.com` (o la IP) desde
**cualquier dispositivo** — tu celular incluido — ya deberías ver la
pantalla de login del sistema.

---

## 10. HTTPS gratis con Let's Encrypt (obligatorio, no opcional)

Sin esto, las contraseñas de Control Escolar viajan sin cifrar por
internet, y varias cosas que ya configuramos (`SESSION_COOKIE_SECURE`)
dejan de funcionar bien. Solo aplica si ya tienes un dominio apuntando al
VPS (no funciona sobre IP sola):

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d TU_DOMINIO.com -d www.TU_DOMINIO.com
```

Certbot te va a preguntar tu correo y si quieres redirigir todo el tráfico
HTTP a HTTPS — di que sí. El certificado se renueva solo (Certbot instala
una tarea automática); no tienes que hacer nada más.

---

## 11. Cómo actualizar el sistema después (flujo normal de trabajo)

Cada vez que tú o tu amigo suban cambios nuevos a GitHub y quieran
reflejarlos en producción:

```bash
ssh sge@IP_DE_TU_VPS
cd ~/sge_enfermeria
source venv/bin/activate

git pull

pip install -r requirements.txt -r requirements-prod.txt   # por si hay dependencias nuevas
flask db upgrade                                            # por si hay modelos nuevos

sudo systemctl restart sge     # reinicia Gunicorn para cargar el código nuevo
```

Nginx no necesita reiniciarse a menos que edites su propio archivo de
configuración.

---

## Checklist de seguridad antes de que Control Escolar empiece a usarlo con datos reales

- [ ] `.env` en el servidor tiene un `SECRET_KEY` distinto al de desarrollo
- [ ] HTTPS activo (candado en el navegador, sin advertencias)
- [ ] Cuenta Directivo creada con contraseña fuerte (no la misma de prueba)
- [ ] `ufw status` muestra el firewall activo
- [ ] `~/.pgpass` configurado con permiso `600` (paso 12) — si no, el respaldo automático nunca corre
- [ ] Respaldos automáticos configurados y probados — ver `deploy/BACKUPS.md`
- [ ] Se hizo UNA restauración de prueba de un respaldo (ver `BACKUPS.md`), no solo el respaldo
- [ ] Se sembró con `python seed.py --produccion` (en `/registro` solo aparece Enfermería)
- [ ] Nombre de la institución configurado (`/configuracion/institucion`): sale en fichas, boletas, recibos, correos y reportes; mientras diga "Mi Institución Educativa" el sistema avisa al Directivo
- [ ] Precios capturados: mensualidad de la carrera, Inscripción y Reinscripción, recargos
- [ ] `redis-server` activo y habilitado (`systemctl enable redis-server`): guarda el límite de intentos de login
- [ ] Reloj sincronizado: `timedatectl` debe decir `System clock synchronized: yes`
- [ ] Aviso de privacidad publicado (el sistema guarda INE, CURP, actas y domicilios de alumnos)

---

## 12. Autenticación de PostgreSQL para el respaldo automático (obligatorio)

`deploy/backup.sh` corre `pg_dump` desde una tarea de cron, y **cron no
tiene terminal** para que alguien teclee la contraseña de PostgreSQL
manualmente. Sin este paso, el respaldo automático falla todas las
noches en silencio (el error solo queda en `backup.log`), y no te
enteras hasta el día que de verdad necesites restaurar algo y no haya
nada que restaurar.

Como el usuario `sge` (el mismo que corre la app, NO root):

```bash
touch ~/.pgpass
chmod 600 ~/.pgpass
```

Edita `~/.pgpass` y agrega esta línea, con tu contraseña real de
`sge_user` (la misma del paso 3):

```
localhost:5432:sge_produccion:sge_user:TU_CONTRASEÑA_REAL_AQUI
```

El permiso `600` es obligatorio — PostgreSQL se niega a leer este
archivo si otros usuarios pueden verlo. Prueba que funcione sin pedir
contraseña:

```bash
pg_dump -U sge_user -h localhost sge_produccion > /dev/null && echo "OK: pg_dump no pidió contraseña"
```

Si no te pidió contraseña y no dio error, ya quedó listo para que
`backup.sh` corra solo desde cron.

---

## Siguiente paso: Respaldos automáticos

Ver `deploy/BACKUPS.md` — cubre respaldo diario de base de datos +
documentos, con copia fuera del VPS (offsite) y cómo restaurar. **No
cargues alumnos reales sin esto configurado primero.**



---

## Anexo A. Servidor en una casa u oficina

Todo lo anterior aplica igual. Lo que cambia es **cómo llegan los usuarios desde
internet**, qué pasa con la luz, y **dónde viven los respaldos**.

### A1. ¿Tiene tu conexión una IP pública?

En el servidor: `curl -s ifconfig.me` y compara con la IP "WAN" que muestra la
página del módem/router. Si son **distintas**, o la WAN empieza con `100.64.`–
`100.127.` o `10.`/`192.168.`, tu proveedor usa **CGNAT** y **no puedes abrir
puertos**: ve directo a la **Opción B**. Si son iguales, usa la **Opción A**.
(Pregunta también al proveedor si su contrato permite servidores en conexión
residencial.)

### A2. Opción A — IP pública

1. **IP fija en la red local** para el servidor (reserva DHCP en el router por su
   MAC, o IP estática en Ubuntu con netplan).
2. En el router, **reenvía los puertos 80 y 443** hacia esa IP local. **Nada más**
   (el 22/SSH no se expone a internet; desactiva UPnP).
3. **Dominio** apuntando a tu IP pública. Si tu IP cambia (lo normal en casa), usa un
   servicio DNS dinámico (DuckDNS, No-IP, o el DNS de tu dominio con actualizador
   como `ddclient`) para que el registro se actualice solo.
4. Sigue el paso 9 (Nginx) y el paso 10 (Certbot) tal cual.

### A3. Opción B — CGNAT o sin acceso al router: Cloudflare Tunnel

Sirve si tu dominio está en Cloudflare (gratis). `cloudflared` instalado en el
servidor abre una conexión SALIENTE hacia Cloudflare y publica tu Nginx local
(`http://localhost:80`) en `https://TU_DOMINIO.com`; **no abres ningún puerto** y
el HTTPS lo pone Cloudflare (no usas Certbot).

- Usa **`deploy/nginx_sge_tunel.conf`** en lugar de `nginx_sge.conf`. Es necesario:
  con un túnel, todas las visitas llegan a Nginx desde `127.0.0.1`, y con la
  configuración normal el límite de intentos de login (5 por minuto **por IP**)
  contaría a TODOS los usuarios como una sola persona: una equivocación de un
  compañero bloquearía el login de todos.
- **Privacidad:** el tráfico (incluidos datos de alumnos) pasa por los servidores de
  Cloudflare, que lo descifran para reenviarlo. Considéralo en tu aviso de privacidad.
- **No se ha probado** esta variante en un servidor real (Nginx y `cloudflared` no
  estaban disponibles donde se validó el sistema): haz la prueba de login y de límite
  de intentos (paso A6) antes de abrirlo a usuarios.

### A4. Energía y arranque

- **No-break (UPS)** de al menos 15-20 minutos para el servidor **y el módem/router**.
  Un corte a media escritura puede corromper la base.
- En el BIOS/UEFI activa **"Restore on AC power loss / Power On"** para que el equipo
  encienda solo cuando vuelva la luz.
- Todos los servicios deben arrancar solos:
  `sudo systemctl enable postgresql redis-server nginx sge` (y `cloudflared` si aplica).
- Vigila el disco: `sudo apt install smartmontools` y `sudo smartctl -H /dev/sda`.

### A5. Seguridad del equipo en casa

- Solo entran por SSH con **llave** (desactiva contraseña: `PasswordAuthentication no`
  en `/etc/ssh/sshd_config`) y `sudo apt install fail2ban unattended-upgrades`.
- El servidor no debe usarse para nada más (ni navegación, ni otras aplicaciones), y
  los demás equipos de la casa **no deben poder leer** sus discos.
- Quien tenga acceso físico o `root` puede leer la base y los documentos: acuerden
  por escrito quién administra, y cifra los respaldos antes de copiarlos fuera.

### A6. Prueba de humo tras publicar (5 minutos)

1. Desde el **celular con datos móviles** (no con tu wifi) abre `https://TU_DOMINIO.com`:
   debe verse el login con candado.
2. Entra con la cuenta Directivo. Si vuelve al login sin error, la cookie segura no
   se guardó: revisa que estés entrando por `https://` y no por `http://` o por IP.
3. Equivócate 6 veces en el login desde una IP y entra después desde **otra** (otro
   celular): la segunda persona debe poder entrar (si no, el límite de intentos está
   contando a todos como uno: revisa la Opción B).
4. `sudo systemctl stop redis-server`, intenta entrar (debe funcionar) y
   `sudo systemctl start redis-server`. Revisa `logs/sge.log` para el aviso.

### A7. Respaldos: la copia en la misma casa no cuenta

Un incendio, robo, falla del disco o ransomware se llevan el servidor **y** sus
respaldos locales. Configura la copia externa (`rclone`, ver `BACKUPS.md`) a un
servicio en la nube o a un disco USB que se **guarde fuera de casa**, y haz al menos
una restauración de prueba antes de cargar alumnos reales.
