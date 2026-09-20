#!/bin/bash
# =============================================================================
# Respaldo del SGE: base de datos (pg_dump) + documentos de alumnos (tar).
#
#   ./backup.sh            respaldo COMPLETO (base + documentos)        -> cron diario, 3:00
#   ./backup.sh --solo-bd  solo la base (rápido; reduce cuánto se pierde) -> cron 12:00 y 18:00
#
# Qué garantiza:
#   * cada archivo se VALIDA (gzip íntegro y dump COMPLETO: no basta con que exista);
#   * los archivos se crean con permiso 600 (contienen datos personales);
#   * la copia EXTERNA (rclone) va CIFRADA con GPG AES-256; sin frase de contraseña NO se sube nada;
#   * si algo falla se deja un archivo ALERTA_ULTIMO_FALLO.txt y se manda correo (ver avisar.py);
#   * rotación por días y por espacio, sin borrar nunca el respaldo recién hecho.
# Variables de entorno opcionales: ver lib_respaldo.sh.
# =============================================================================
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib_respaldo.sh
source "$SCRIPT_DIR/lib_respaldo.sh"

RETENTION_DIAS="${RETENTION_DIAS:-14}"              # días de respaldos LOCALES
RETENTION_MAX_MB="${RETENTION_MAX_MB:-8000}"        # tope duro de espacio local (8 GB)
RETENTION_EXTERNO_DIAS="${RETENTION_EXTERNO_DIAS:-60}"
RCLONE_DESTINO="${RCLONE_DESTINO:-backup:sge-backups/}"
SOLO_BD=0; [ "${1:-}" = "--solo-bd" ] && SOLO_BD=1

trap 'alertar "El respaldo FALLÓ" "Falló el comando de la línea $LINENO de backup.sh. Revisa $LOG_FILE."' ERR

FECHA=$(date +%Y-%m-%d_%H-%M-%S)
log "--- Iniciando backup ($([ "$SOLO_BD" = 1 ] && echo solo base de datos || echo completo)) ---"
exigir_pgpass
pg_conexion

# ---------------------------------------------------------------- 1. Base de datos
DB_BACKUP_FILE="$BACKUP_DIR/db_${FECHA}.sql.gz"
# --no-owner/--no-privileges: el dump se puede restaurar en un servidor nuevo con otro usuario de BD.
if pg_dump --clean --if-exists --no-owner --no-privileges "${PG_OPTS[@]}" "$DB_NAME" | gzip > "$DB_BACKUP_FILE"; then
    :
else
    rm -f "$DB_BACKUP_FILE"
    alertar "Falló pg_dump" "No se pudo respaldar la base $DB_NAME."
    exit 1
fi
# Íntegro Y completo: pg_dump termina siempre con esta línea; si falta, el dump se cortó a la mitad.
if ! gzip -t "$DB_BACKUP_FILE" 2>>"$LOG_FILE" || ! gunzip -c "$DB_BACKUP_FILE" | tail -n 5 | grep -q "PostgreSQL database dump complete"; then
    rm -f "$DB_BACKUP_FILE"
    alertar "Respaldo de la base corrupto o incompleto" "Se eliminó $DB_BACKUP_FILE (gzip inválido o dump cortado)."
    exit 1
fi
log "OK: Base de datos respaldada ($(du -h "$DB_BACKUP_FILE" | cut -f1)) -> $DB_BACKUP_FILE"
date -Iseconds > "$BACKUP_DIR/ULTIMO_RESPALDO_BD_OK"

# ---------------------------------------------------------------- 2. Documentos
UPLOADS_BACKUP_FILE=""
if [ "$SOLO_BD" = 0 ]; then
    if [ -d "$APP_DIR/instance/documentos_alumnos" ]; then
        UPLOADS_BACKUP_FILE="$BACKUP_DIR/documentos_${FECHA}.tar.gz"
        if ! tar -czf "$UPLOADS_BACKUP_FILE" -C "$APP_DIR/instance" documentos_alumnos; then
            rm -f "$UPLOADS_BACKUP_FILE"
            alertar "Falló el respaldo de documentos" "tar no pudo empaquetar instance/documentos_alumnos."
            exit 1
        fi
        if ! tar -tzf "$UPLOADS_BACKUP_FILE" > /dev/null 2>>"$LOG_FILE"; then
            rm -f "$UPLOADS_BACKUP_FILE"
            alertar "Respaldo de documentos corrupto" "Se eliminó $UPLOADS_BACKUP_FILE."
            exit 1
        fi
        log "OK: Documentos respaldados ($(du -h "$UPLOADS_BACKUP_FILE" | cut -f1)) -> $UPLOADS_BACKUP_FILE"
        date -Iseconds > "$BACKUP_DIR/ULTIMO_RESPALDO_DOCS_OK"
    else
        log "AVISO: aún no existe carpeta de documentos, se omite ese respaldo."
    fi
fi

# ---------------------------------------------------------------- 3. Copia EXTERNA (cifrada)
if rclone_configurado; then
    if [ -s "$BACKUP_PASSPHRASE_FILE" ]; then
        for original in "$DB_BACKUP_FILE" ${UPLOADS_BACKUP_FILE:+"$UPLOADS_BACKUP_FILE"}; do
            cifrado="${original}.gpg"
            gpg --batch --yes --quiet --symmetric --cipher-algo AES256 --passphrase-file "$BACKUP_PASSPHRASE_FILE" -o "$cifrado" "$original"
            rclone copy "$cifrado" "$RCLONE_DESTINO" >> "$LOG_FILE" 2>&1
            rm -f "$cifrado"
        done
        log "OK: Copia externa CIFRADA subida a $RCLONE_DESTINO"
        rclone delete "$RCLONE_DESTINO" --min-age "${RETENTION_EXTERNO_DIAS}d" >> "$LOG_FILE" 2>&1 || true
        date -Iseconds > "$BACKUP_DIR/ULTIMA_COPIA_EXTERNA_OK"
    else
        alertar "Copia externa NO subida" "Hay rclone configurado pero falta la frase de contraseña ($BACKUP_PASSPHRASE_FILE). Por seguridad no se sube nada sin cifrar."
        exit 1
    fi
else
    log "AVISO IMPORTANTE: rclone no está configurado. Este respaldo SOLO está en este equipo:"
    log "  si el disco falla o hay robo/incendio, se pierde junto con la base. Ver BACKUPS.md."
fi

# ---------------------------------------------------------------- 4. Rotación local
find "$BACKUP_DIR" -name "db_*.sql.gz" -mtime +"$RETENTION_DIAS" -delete
find "$BACKUP_DIR" -name "documentos_*.tar.gz" -mtime +"$RETENTION_DIAS" -delete
while true; do
    USADO_MB=$(du -sm "$BACKUP_DIR" | cut -f1)
    [ "$USADO_MB" -le "$RETENTION_MAX_MB" ] && break
    MAS_VIEJO=$(ls -1tr "$BACKUP_DIR"/db_*.sql.gz "$BACKUP_DIR"/documentos_*.tar.gz 2>/dev/null | head -1 || true)
    if [ -z "$MAS_VIEJO" ] || [ "$MAS_VIEJO" = "$DB_BACKUP_FILE" ] || [ "$MAS_VIEJO" = "$UPLOADS_BACKUP_FILE" ]; then
        log "AVISO: la carpeta usa ${USADO_MB}MB (tope ${RETENTION_MAX_MB}MB) y ya no hay respaldos viejos que borrar. Amplía el disco."
        break
    fi
    rm -f "$MAS_VIEJO"
    log "Rotación por espacio: eliminado $MAS_VIEJO (uso ${USADO_MB}MB > ${RETENTION_MAX_MB}MB)"
done

trap - ERR
log "--- Backup completado ---"
