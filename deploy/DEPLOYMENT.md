# Guía de Despliegue — Sistema de Gestión Escolar (SGE)

Esta guía asume un VPS con **Ubuntu 24.04** (o 22.04, es casi idéntico) y que
ya tienes acceso por SSH como root o con un usuario con `sudo`.

Reemplaza `TU_DOMINIO.com` y `tu_usuario_github` por los tuyos reales en
todos los comandos.

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
\q
```

Guarda esa contraseña — la vas a necesitar en el `.env` del paso 5.

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
python seed.py
python crear_admin.py
```

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
- [ ] Respaldos automáticos configurados y probados — ver `deploy/BACKUPS.md`

---

## Siguiente paso: Respaldos automáticos

Ver `deploy/BACKUPS.md` — cubre respaldo diario de base de datos +
documentos, con copia fuera del VPS (offsite) y cómo restaurar. **No
cargues alumnos reales sin esto configurado primero.**

