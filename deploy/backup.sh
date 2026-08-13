#!/bin/bash
set -euo pipefail

# ============================================================
# Backup script - Sistema de Gestión Escolar (SGE)
#
# Respalda:
#   1. La base de datos PostgreSQL completa (pg_dump)
#   2. Los documentos digitalizados subidos (instance/documentos_alumnos/)
#
# Guarda los respaldos localmente en el VPS, y si rclone está
# configurado (ver BACKUPS.md), también los copia a almacenamiento
# externo — esto es lo que te protege si el VPS completo falla o
# se pierde, no solo si se corrompe la base de datos.
#
# Diseñado para correr solo, todos los días, vía cron.
#
# ------------------------------------------------------------
# CAMBIOS DE LA AUDITORÍA (no revertir sin leer esto):
#
# [D2] AUTENTICACIÓN EN CRON. pg_dump con "-h localhost" abre una
#      conexión TCP, y PostgreSQL entonces pide CONTRASEÑA (scram/md5),
#      no autenticación "peer" del sistema. Cuando corres el script a
#      mano, tú la escribes y no se nota el problema; pero cron NO tiene
#      terminal, así que ahí el respaldo fallaba TODAS las noches en
#      silencio. La solución es ~/.pgpass (ver comprobación abajo y las
#      instrucciones en BACKUPS.md).
#
# [D3] --clean --if-exists. Sin esto, el .sql generado NO contiene
#      instrucciones DROP, así que al restaurarlo sobre una base que ya
#      tiene tablas, todo falla con "ya existe" y la restauración queda
#      a medias. Es decir: el respaldo existía, pero NO era recuperable.
#
# [D10] RETENCIÓN POR ESPACIO, no solo por días. 14 copias completas de
#      documentos escaneados pueden llenar el disco del VPS; si el disco
#      se llena, se cae PostgreSQL Y dejan de correr los respaldos, todo
#      al mismo tiempo.
#
# También se agregó verificación de integridad: un respaldo que no se
# puede descomprimir no sirve de nada, y es mejor enterarse ahora que
# el día de la emergencia.
# ============================================================

# --- Configuración: AJUSTA estos valores a tu servidor real ---
APP_DIR="/home/sge/sge_enfermeria"
BACKUP_DIR="/home/sge/backups"
RETENTION_DIAS=14          # Cuántos días de respaldos LOCALES conservar
RETENTION_MAX_MB=8000      # Tope duro de espacio para la carpeta de respaldos (8 GB)
DB_NAME="sge_produccion"
DB_USER="sge_user"

# [D2] Ruta explícita al archivo de contraseñas. cron arranca con un
# entorno mínimo; no des por hecho que $HOME apunta a donde crees.
export PGPASSFILE="/home/sge/.pgpass"

FECHA=$(date +%Y-%m-%d_%H-%M-%S)
LOG_FILE="$BACKUP_DIR/backup.log"

mkdir -p "$BACKUP_DIR"

log() {
    echo "[$(date +%Y-%m-%d_%H-%M-%S)] $*" >> "$LOG_FILE"
}

log "--- Iniciando backup ---"

# --- 0. Comprobación previa: ¿puede autenticarse este script? [D2] ---
# Falla AQUÍ, con un mensaje claro, en vez de fallar a media noche sin
# que nadie sepa por qué.
if [ ! -f "$PGPASSFILE" ]; then
    log "ERROR CRÍTICO: no existe $PGPASSFILE."
    log "  Sin ese archivo, pg_dump no puede autenticarse cuando el script"
    log "  corre desde cron (no hay terminal donde escribir la contraseña)."
    log "  Créalo así (una sola vez, como usuario sge):"
    log "    echo 'localhost:5432:$DB_NAME:$DB_USER:TU_PASSWORD' > ~/.pgpass"
    log "    chmod 600 ~/.pgpass"
    exit 1
fi

# PostgreSQL IGNORA el archivo .pgpass si tiene permisos demasiado
# abiertos (es una protección suya, no un capricho). Si pasa eso, el
# respaldo fallaría igual que si no existiera.
PERMISOS_PGPASS=$(stat -c '%a' "$PGPASSFILE")
if [ "$PERMISOS_PGPASS" != "600" ]; then
    log "ERROR CRÍTICO: $PGPASSFILE tiene permisos $PERMISOS_PGPASS (deben ser 600)."
    log "  PostgreSQL ignora el archivo si es legible por otros usuarios."
    log "  Corrígelo con: chmod 600 $PGPASSFILE"
    exit 1
fi

# --- 1. Dump de la base de datos, comprimido ---
DB_BACKUP_FILE="$BACKUP_DIR/db_${FECHA}.sql.gz"

# [D3] --clean --if-exists hace que el dump incluya los DROP necesarios,
# para que restaurarlo sobre una base existente funcione de verdad.
if pg_dump --clean --if-exists -U "$DB_USER" -h localhost "$DB_NAME" | gzip > "$DB_BACKUP_FILE"; then
    TAMANO=$(du -h "$DB_BACKUP_FILE" | cut -f1)
    log "OK: Base de datos respaldada ($TAMANO) -> $DB_BACKUP_FILE"
else
    log "ERROR: Falló el respaldo de la base de datos. Abortando."
    rm -f "$DB_BACKUP_FILE"   # no dejar un archivo a medias que parezca válido
    exit 1
fi

# Verificar que el archivo se pueda descomprimir de verdad.
if ! gzip -t "$DB_BACKUP_FILE" 2>>"$LOG_FILE"; then
    log "ERROR: El respaldo de base de datos quedó CORRUPTO (gzip -t falló). Se elimina."
    rm -f "$DB_BACKUP_FILE"
    exit 1
fi

# Un dump válido de este proyecto nunca es diminuto. Si lo es, algo salió
# mal aunque pg_dump haya devuelto éxito (ej. base vacía por error).
TAMANO_BYTES=$(stat -c '%s' "$DB_BACKUP_FILE")
if [ "$TAMANO_BYTES" -lt 1024 ]; then
    log "AVISO: el respaldo pesa solo $TAMANO_BYTES bytes -- revisa que la base"
    log "  de datos realmente tenga información."
fi

# --- 2. Comprimir la carpeta de documentos subidos ---
# SECURITY-NOTE: esta ruta cambió de static/uploads/ a instance/documentos_alumnos/
# porque los documentos de alumnos (INE, CURP, actas) ya NO viven dentro de
# static/ -- static/ se sirve públicamente sin login (tanto por Flask como por
# el alias /static/ de Nginx), así que había que sacarlos de ahí.
UPLOADS_BACKUP_FILE="$BACKUP_DIR/documentos_${FECHA}.tar.gz"

if [ -d "$APP_DIR/instance/documentos_alumnos" ]; then
    if tar -czf "$UPLOADS_BACKUP_FILE" -C "$APP_DIR/instance" documentos_alumnos; then
        TAMANO=$(du -h "$UPLOADS_BACKUP_FILE" | cut -f1)
        log "OK: Documentos respaldados ($TAMANO) -> $UPLOADS_BACKUP_FILE"
    else
        log "ERROR: Falló el respaldo de documentos."
        rm -f "$UPLOADS_BACKUP_FILE"
        exit 1
    fi

    # Verificar que el .tar.gz se pueda leer completo. Esto es justo lo
    # que restore.sh comprueba antes de tocar nada -- mejor detectarlo
    # aquí, el día que se creó, que el día que se necesita.
    if ! tar -tzf "$UPLOADS_BACKUP_FILE" > /dev/null 2>>"$LOG_FILE"; then
        log "ERROR: El respaldo de documentos quedó CORRUPTO. Se elimina."
        rm -f "$UPLOADS_BACKUP_FILE"
        exit 1
    fi
else
    log "AVISO: aún no existe carpeta de documentos, se omite este respaldo."
    UPLOADS_BACKUP_FILE=""
fi

# --- 3. Copia OFFSITE (fuera del VPS) con rclone, si está configurado ---
# Ver BACKUPS.md para configurar el remoto llamado "backup" la primera vez.
if command -v rclone >/dev/null 2>&1 && rclone listremotes 2>/dev/null | grep -q "^backup:"; then
    rclone copy "$DB_BACKUP_FILE" backup:sge-backups/ >> "$LOG_FILE" 2>&1
    if [ -n "$UPLOADS_BACKUP_FILE" ]; then
        rclone copy "$UPLOADS_BACKUP_FILE" backup:sge-backups/ >> "$LOG_FILE" 2>&1
    fi
    log "OK: Copiado a almacenamiento externo (rclone)."
else
    log "AVISO IMPORTANTE: rclone no está configurado todavía."
    log "  Este backup SOLO quedó en el propio VPS -> si el disco"
    log "  falla, este respaldo se pierde también. Configura el"
    log "  remoto 'backup' siguiendo BACKUPS.md cuanto antes."
fi

# --- 4. Rotación por ANTIGÜEDAD: borrar respaldos más viejos que N días ---
# (los respaldos remotos, si usas rclone, no se borran aquí — configura su
#  propia política de retención del lado del proveedor si lo necesitas)
find "$BACKUP_DIR" -name "db_*.sql.gz" -mtime +$RETENTION_DIAS -delete
find "$BACKUP_DIR" -name "documentos_*.tar.gz" -mtime +$RETENTION_DIAS -delete

# --- 5. Rotación por ESPACIO [D10] ---
# La rotación por días sola no basta: si los documentos escaneados crecen,
# 14 días de respaldos pueden llenar el disco. Un disco lleno tumba
# PostgreSQL y los respaldos al mismo tiempo -- el peor momento posible.
# Aquí borramos del más viejo al más nuevo hasta volver bajo el tope,
# NUNCA el que acabamos de crear.
while true; do
    USADO_MB=$(du -sm "$BACKUP_DIR" | cut -f1)
    if [ "$USADO_MB" -le "$RETENTION_MAX_MB" ]; then
        break
    fi

    MAS_VIEJO=$(ls -1tr "$BACKUP_DIR"/db_*.sql.gz "$BACKUP_DIR"/documentos_*.tar.gz 2>/dev/null | head -1 || true)

    # Sin nada más que borrar, o si lo único que queda es el respaldo de
    # hoy: detenerse. Preferimos pasarnos del tope antes que quedarnos
    # sin ningún respaldo.
    if [ -z "$MAS_VIEJO" ] || [ "$MAS_VIEJO" = "$DB_BACKUP_FILE" ] || [ "$MAS_VIEJO" = "$UPLOADS_BACKUP_FILE" ]; then
        log "AVISO: la carpeta usa ${USADO_MB}MB (tope ${RETENTION_MAX_MB}MB), pero ya"
        log "  no hay respaldos antiguos que borrar. Amplía el disco o baja"
        log "  RETENTION_DIAS."
        break
    fi

    rm -f "$MAS_VIEJO"
    log "Rotación por espacio: eliminado $MAS_VIEJO (uso ${USADO_MB}MB > ${RETENTION_MAX_MB}MB)"
done

log "--- Backup completado ---"
