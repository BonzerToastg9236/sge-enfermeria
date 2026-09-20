#!/bin/bash
# Funciones comunes de backup.sh, verificar_respaldo.sh y restore.sh (se cargan con `source`).
# Todo lo configurable se puede sobrescribir con variables de entorno (útil para PROBAR los scripts
# sin tocar producción); los valores por defecto son los del servidor descrito en DEPLOYMENT.md.

APP_DIR="${APP_DIR:-/home/sge/sge_enfermeria}"
BACKUP_DIR="${BACKUP_DIR:-/home/sge/backups}"
DB_NAME="${DB_NAME:-sge_produccion}"
DB_USER="${DB_USER:-sge_user}"
DB_HOST="${DB_HOST:-localhost}"          # "localhost" = TCP con contraseña (~/.pgpass); una ruta que empiece con "/" = socket Unix
export PGPASSFILE="${PGPASSFILE:-/home/sge/.pgpass}"
# Frase de contraseña con la que se CIFRA la copia externa (GPG simétrico AES-256). Sin ella no se sube nada fuera.
BACKUP_PASSPHRASE_FILE="${BACKUP_PASSPHRASE_FILE:-/home/sge/.sge_backup_passphrase}"

mkdir -p "$BACKUP_DIR"
LOG_FILE="$BACKUP_DIR/backup.log"

log() {
    echo "[$(date +%Y-%m-%d_%H-%M-%S)] $*" >> "$LOG_FILE"
}

# Avisa de un fallo: log + archivo de alerta visible + correo si está configurado (ALERTA_CORREO en .env).
alertar() {
    local asunto="$1" mensaje="$2"
    log "ERROR: $asunto -- $mensaje"
    printf '%s\n%s\n%s\n' "$(date -Iseconds)" "$asunto" "$mensaje" > "$BACKUP_DIR/ALERTA_ULTIMO_FALLO.txt"
    local py="$APP_DIR/venv/bin/python"; [ -x "$py" ] || py="$(command -v python3 || true)"
    if [ -n "$py" ] && [ -f "$APP_DIR/deploy/avisar.py" ]; then
        APP_DIR="$APP_DIR" BACKUP_DIR="$BACKUP_DIR" "$py" "$APP_DIR/deploy/avisar.py" "$asunto" "$mensaje" >> "$LOG_FILE" 2>&1 || true
    fi
}

# Opciones de conexión de PostgreSQL. Con socket Unix (DB_HOST="/ruta") no hay contraseña de por medio.
pg_conexion() {
    PG_OPTS=(-U "$DB_USER" -h "$DB_HOST")
}

# Con conexión TCP (cron no tiene terminal) hace falta ~/.pgpass con permiso 600.
exigir_pgpass() {
    case "$DB_HOST" in
        /*) return 0 ;;
    esac
    if [ ! -f "$PGPASSFILE" ]; then
        alertar "Falta $PGPASSFILE" "pg_dump/psql no pueden autenticarse desde cron. Crea el archivo (ver DEPLOYMENT.md, sección 12)."
        exit 1
    fi
    if [ "$(stat -c '%a' "$PGPASSFILE")" != "600" ]; then
        alertar "$PGPASSFILE con permisos incorrectos" "Deben ser 600 (chmod 600 $PGPASSFILE); PostgreSQL ignora el archivo si otros pueden leerlo."
        exit 1
    fi
}

# ¿Hay un remoto rclone configurado? (RCLONE_DESTINO, p. ej. "backup:sge-backups/")
rclone_configurado() {
    command -v rclone >/dev/null 2>&1 || return 1
    local remoto="${RCLONE_DESTINO:-backup:sge-backups/}"; remoto="${remoto%%:*}:"
    rclone listremotes 2>/dev/null | grep -qx "$remoto"
}
