#!/bin/bash
set -euo pipefail

# ============================================================
# Backup script - Sistema de Gestión Escolar (SGE)
#
# Respalda:
#   1. La base de datos PostgreSQL completa (pg_dump)
#   2. Los documentos digitalizados subidos (static/uploads/)
#
# Guarda los respaldos localmente en el VPS, y si rclone está
# configurado (ver BACKUPS.md), también los copia a almacenamiento
# externo — esto es lo que te protege si el VPS completo falla o
# se pierde, no solo si se corrompe la base de datos.
#
# Diseñado para correr solo, todos los días, vía cron.
# ============================================================

# --- Configuración: AJUSTA estos valores a tu servidor real ---
APP_DIR="/home/sge/sge_enfermeria"
BACKUP_DIR="/home/sge/backups"
RETENTION_DIAS=14          # Cuántos días de respaldos LOCALES conservar
DB_NAME="sge_produccion"
DB_USER="sge_user"

FECHA=$(date +%Y-%m-%d_%H-%M-%S)
LOG_FILE="$BACKUP_DIR/backup.log"

mkdir -p "$BACKUP_DIR"

echo "[$FECHA] --- Iniciando backup ---" >> "$LOG_FILE"

# --- 1. Dump de la base de datos, comprimido ---
DB_BACKUP_FILE="$BACKUP_DIR/db_${FECHA}.sql.gz"

if pg_dump -U "$DB_USER" -h localhost "$DB_NAME" | gzip > "$DB_BACKUP_FILE"; then
    TAMANO=$(du -h "$DB_BACKUP_FILE" | cut -f1)
    echo "[$FECHA] OK: Base de datos respaldada ($TAMANO) -> $DB_BACKUP_FILE" >> "$LOG_FILE"
else
    echo "[$FECHA] ERROR: Falló el respaldo de la base de datos. Abortando." >> "$LOG_FILE"
    exit 1
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
        echo "[$FECHA] OK: Documentos respaldados ($TAMANO) -> $UPLOADS_BACKUP_FILE" >> "$LOG_FILE"
    else
        echo "[$FECHA] ERROR: Falló el respaldo de documentos." >> "$LOG_FILE"
        exit 1
    fi
else
    echo "[$FECHA] AVISO: aún no existe carpeta de documentos, se omite este respaldo." >> "$LOG_FILE"
    UPLOADS_BACKUP_FILE=""
fi

# --- 3. Copia OFFSITE (fuera del VPS) con rclone, si está configurado ---
# Ver BACKUPS.md para configurar el remoto llamado "backup" la primera vez.
if command -v rclone >/dev/null 2>&1 && rclone listremotes 2>/dev/null | grep -q "^backup:"; then
    rclone copy "$DB_BACKUP_FILE" backup:sge-backups/ >> "$LOG_FILE" 2>&1
    if [ -n "$UPLOADS_BACKUP_FILE" ]; then
        rclone copy "$UPLOADS_BACKUP_FILE" backup:sge-backups/ >> "$LOG_FILE" 2>&1
    fi
    echo "[$FECHA] OK: Copiado a almacenamiento externo (rclone)." >> "$LOG_FILE"
else
    echo "[$FECHA] AVISO IMPORTANTE: rclone no está configurado todavía." >> "$LOG_FILE"
    echo "[$FECHA]   Este backup SOLO quedó en el propio VPS -> si el disco" >> "$LOG_FILE"
    echo "[$FECHA]   falla, este respaldo se pierde también. Configura el" >> "$LOG_FILE"
    echo "[$FECHA]   remoto 'backup' siguiendo BACKUPS.md cuanto antes." >> "$LOG_FILE"
fi

# --- 4. Rotación: borrar respaldos LOCALES más viejos que RETENTION_DIAS ---
# (los respaldos remotos, si usas rclone, no se borran aquí — configura su
#  propia política de retención del lado del proveedor si lo necesitas)
find "$BACKUP_DIR" -name "db_*.sql.gz" -mtime +$RETENTION_DIAS -delete
find "$BACKUP_DIR" -name "documentos_*.tar.gz" -mtime +$RETENTION_DIAS -delete

echo "[$FECHA] --- Backup completado ---" >> "$LOG_FILE"
