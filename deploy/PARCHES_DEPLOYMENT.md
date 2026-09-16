# Parches de despliegue — correcciones D1 a D10

Este documento contiene los cambios que hay que hacer **a mano** dentro de
`DEPLOYMENT.md` y `BACKUPS.md` (son ediciones puntuales dentro de guías
largas, por eso no se entregan como archivo completo), más el orden en que
conviene instalar los archivos ya corregidos.

Los archivos `backup.sh`, `restore.sh` y `logrotate_sge` **ya vienen
corregidos** — solo hay que copiarlos en su lugar.

---

## 1. [D1] `DEPLOYMENT.md` — paso 3, permisos de PostgreSQL

### El problema

La guía indica:

```sql
CREATE DATABASE sge_produccion;
CREATE USER sge_user WITH PASSWORD '...';
GRANT ALL PRIVILEGES ON DATABASE sge_produccion TO sge_user;
```

Eso funcionaba en PostgreSQL 14 y anteriores. **Desde PostgreSQL 15 ya no
basta**: el permiso de crear objetos dentro del esquema `public` se le quitó
al rol `PUBLIC`, y un `GRANT` a nivel de *base de datos* no otorga permisos
sobre el *esquema*.

Como `DEPLOYMENT.md` recomienda Ubuntu 24.04, que instala **PostgreSQL 16**,
esto no es hipotético: el paso 6 (`flask db upgrade`) fallaría con

```
ERROR: permission denied for schema public
```

justo donde se crean todas las tablas del sistema. Te quedarías atorado en
el primer despliegue con un error que no es obvio si no conoces el cambio.

### La corrección

Reemplaza ese bloque SQL del paso 3 por este:

```sql
CREATE DATABASE sge_produccion;
CREATE USER sge_user WITH PASSWORD 'ELIGE_UNA_CONTRASEÑA_FUERTE_AQUI';
GRANT ALL PRIVILEGES ON DATABASE sge_produccion TO sge_user;

-- NECESARIO EN PostgreSQL 15+ (Ubuntu 24.04 trae PG16):
-- El GRANT de arriba da permisos sobre la BASE DE DATOS, pero no sobre el
-- ESQUEMA public. Sin estas dos líneas, "flask db upgrade" del paso 6
-- falla con "permission denied for schema public".
\c sge_produccion
GRANT ALL ON SCHEMA public TO sge_user;
ALTER DATABASE sge_produccion OWNER TO sge_user;

\q
```

### Cómo verificar que quedó bien

Antes de seguir al paso 4, comprueba que el usuario realmente puede crear
tablas:

```bash
psql -U sge_user -h localhost -d sge_produccion -c "CREATE TABLE prueba_permisos (id int); DROP TABLE prueba_permisos;"
```

Si no da error, los permisos están correctos.

---

## 2. [D5] `DEPLOYMENT.md` — paso 5, restricción en la contraseña

### El problema

`sge.service` carga el `.env` con `EnvironmentFile=`. systemd **no
interpreta ese archivo igual que bash**: trata `#` como inicio de comentario
y no procesa comillas de la misma forma.

Si la contraseña de PostgreSQL contiene `#`, la línea

```
DATABASE_URL=postgresql://sge_user:pa#ssword@localhost:5432/sge_produccion
```

llegaría **truncada** a Gunicorn (`postgresql://sge_user:pa`), y la
aplicación fallaría al conectar con un error confuso que parece de red o de
credenciales.

Los caracteres `@`, `/` y `:` causan un problema distinto pero igual de real:
son separadores dentro de la propia URL de conexión.

### La corrección

Agrega esta nota justo después del bloque `.env` del paso 5:

> **Importante sobre la contraseña de PostgreSQL:** úsala **solo con letras
> y números** (sin `#`, `@`, `/`, `:`, comillas ni espacios). Dos motivos:
> systemd corta la línea del `.env` al encontrar un `#`, y los otros
> caracteres son separadores dentro de la propia `DATABASE_URL`. Una
> contraseña larga alfanumérica es igual de segura y evita ambos problemas:
>
> ```bash
> python3 -c "import secrets; print(secrets.token_urlsafe(24).replace('-','').replace('_',''))"
> ```

---

## 3. [D2] `BACKUPS.md` — crear `~/.pgpass` (paso nuevo, obligatorio)

### El problema

`backup.sh` corre `pg_dump -U sge_user -h localhost`. El `-h localhost`
fuerza una conexión TCP, y ahí PostgreSQL pide **contraseña** (autenticación
scram/md5), no autenticación `peer` del sistema operativo.

Cuando corres el script a mano, tú escribes la contraseña y todo parece
funcionar. Pero **cron no tiene terminal**: el respaldo automático de las
3:00 AM fallaría todas las noches. El script sí lo registraba en el log,
pero `BACKUPS.md` sugiere revisarlo *"una vez a la semana"* — así que lo
más probable es que te enteraras el día que necesitaras restaurar.

### La corrección

Agrega esta sección en `BACKUPS.md`, **antes** de "Instalación en el VPS":

> ### Paso obligatorio: credenciales para que cron pueda respaldar
>
> El respaldo corre de madrugada, sin nadie frente al teclado, así que
> PostgreSQL necesita poder autenticarse sin preguntar nada. Eso se hace
> con un archivo `~/.pgpass`:
>
> ```bash
> echo 'localhost:5432:sge_produccion:sge_user:TU_PASSWORD_REAL' > ~/.pgpass
> chmod 600 ~/.pgpass
> ```
>
> El `chmod 600` no es opcional: **PostgreSQL ignora el archivo si otros
> usuarios pueden leerlo**, y el respaldo fallaría igual que si no
> existiera. El script `backup.sh` verifica ambas cosas al arrancar y se
> detiene con un mensaje claro si algo está mal.
>
> Comprueba que funcione (no debe pedirte contraseña):
>
> ```bash
> psql -U sge_user -h localhost -d sge_produccion -c "SELECT 1;"
> ```

---

## 4. Instalar los archivos corregidos

```bash
cd ~/sge_enfermeria

# Los scripts corregidos ya vienen con los fixes D2, D3, D4 y D10
chmod +x deploy/backup.sh deploy/restore.sh

# [D9] Rotación de los logs de Gunicorn
sudo cp deploy/logrotate_sge /etc/logrotate.d/sge
sudo chown root:root /etc/logrotate.d/sge
sudo chmod 644 /etc/logrotate.d/sge
sudo logrotate -d /etc/logrotate.d/sge   # valida sin aplicar nada
```

---

## 5. La prueba que de verdad importa: restaurar

Esta es la única forma de saber si los respaldos sirven. `BACKUPS.md` ya lo
dice bien: *"un respaldo que nunca has probado a restaurar no es un respaldo
confiable, es una suposición"*.

**Hazlo en un VPS de prueba, nunca en producción.**

```bash
# 1. Generar un respaldo
./deploy/backup.sh
cat ~/backups/backup.log      # debe decir OK, sin ERROR

# 2. Confirmar que los archivos existen y pesan algo razonable
ls -lh ~/backups/

# 3. Restaurarlo (en el servidor de PRUEBA)
./deploy/restore.sh ~/backups/db_FECHA.sql.gz ~/backups/documentos_FECHA.tar.gz

# 4. Entrar al sistema por el navegador y verificar:
#    - Puedes iniciar sesión
#    - Los alumnos aparecen en el buscador
#    - Un expediente abre y muestra sus documentos escaneados
#    - La boleta de un alumno muestra sus calificaciones
```

El paso 4 es el que realmente valida el respaldo. Que el script imprima
"✅ Restauración completa" ya significa más que antes (ahora `psql` se
detiene ante errores en vez de reportar éxito sobre una restauración rota),
pero solo abrir el sistema y ver los datos confirma que la recuperación
sirve de verdad.

---

## Resumen de qué resuelve cada cambio

| ID | Problema | Dónde se corrige | Prioridad |
|---|---|---|---|
| D1 | `flask db upgrade` falla en PostgreSQL 15+ | `DEPLOYMENT.md` paso 3 (manual) | **P0** |
| D2 | El respaldo automático nunca corre desde cron | `~/.pgpass` + `BACKUPS.md` (manual) | **P0** |
| D3 | La restauración parece exitosa sin serlo | `backup.sh` + `restore.sh` (ya corregidos) | **P0** |
| D4 | Se borran documentos antes de validar el respaldo | `restore.sh` (ya corregido) | **P1** |
| D5 | Contraseña con `#` rompe el arranque de Gunicorn | `DEPLOYMENT.md` paso 5 (manual) | **P1** |
| D9 | Logs de Gunicorn llenan el disco | `logrotate_sge` (ya corregido) | **P1** |
| D10 | Respaldos sin tope de espacio | `backup.sh` (ya corregido) | **P1** |
| D7 | Sin cabeceras de seguridad en las respuestas HTTP | `nginx_sge.conf` (**Aplicado**, commit `dfc9da9`, 2026-08-13) | P1 |
| D8 | Gunicorn corre sin ningún aislamiento del sistema | `sge.service` (**Aplicado**, commit `dfc9da9`, 2026-08-13) | P1 |

Verificado en la auditoría de seguridad más reciente (2026-09-15): D7 y D8
ya estaban bien implementados en ambos archivos -- no quedan pendientes de
este paquete.
