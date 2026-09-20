#!/bin/bash
# =============================================================================
# Verifica que los respaldos SIRVAN (un respaldo que nunca se restauró es solo una suposición):
#
#   ./verificar_respaldo.sh --frescura   diario:  ¿hay respaldo reciente, íntegro y con copia externa al día?
#   ./verificar_respaldo.sh              semanal: además RESTAURA el último dump en una base temporal
#                                        y compara sus tablas con la base en producción.
# Si algo está mal deja ALERTA_ULTIMO_FALLO.txt y manda correo (ver avisar.py).
# La prueba completa necesita que el usuario de BD pueda crear bases:
#     sudo -u postgres psql -c "ALTER USER sge_user CREATEDB;"
# =============================================================================
set -euo pipefail
umask 077
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib_respaldo.sh"

MAX_EDAD_HORAS="${MAX_EDAD_HORAS:-26}"
SOLO_FRESCURA=0; [ "${1:-}" = "--frescura" ] && SOLO_FRESCURA=1
trap 'alertar "La verificación del respaldo FALLÓ" "Falló el comando de la línea $LINENO de verificar_respaldo.sh."' ERR

edad_horas() { echo $(( ( $(date +%s) - $(stat -c %Y "$1") ) / 3600 )); }

log "--- Verificando respaldos ---"
ultimo_db=$(ls -1t "$BACKUP_DIR"/db_*.sql.gz 2>/dev/null | head -1 || true)
if [ -z "$ultimo_db" ]; then
    alertar "No existe NINGÚN respaldo de la base" "No hay db_*.sql.gz en $BACKUP_DIR. Corre backup.sh y revisa el cron."
    exit 1
fi
if [ "$(edad_horas "$ultimo_db")" -gt "$MAX_EDAD_HORAS" ]; then
    alertar "Respaldo de la base DESACTUALIZADO" "El último es $ultimo_db, de hace $(edad_horas "$ultimo_db") h (máximo $MAX_EDAD_HORAS h). El cron de backup.sh no está corriendo."
    exit 1
fi
if ! gzip -t "$ultimo_db" || ! gunzip -c "$ultimo_db" | tail -n 5 | grep -q "PostgreSQL database dump complete"; then
    alertar "El último respaldo de la base está CORRUPTO o incompleto" "$ultimo_db no pasó la validación."
    exit 1
fi

if [ -d "$APP_DIR/instance/documentos_alumnos" ]; then
    ultimo_docs=$(ls -1t "$BACKUP_DIR"/documentos_*.tar.gz 2>/dev/null | head -1 || true)
    if [ -z "$ultimo_docs" ] || [ "$(edad_horas "$ultimo_docs")" -gt $((MAX_EDAD_HORAS + 2)) ] || ! tar -tzf "$ultimo_docs" >/dev/null 2>&1; then
        alertar "Respaldo de DOCUMENTOS ausente, viejo o corrupto" "Revisa ${ultimo_docs:-que exista un documentos_*.tar.gz en $BACKUP_DIR}."
        exit 1
    fi
fi

if rclone_configurado; then
    marca="$BACKUP_DIR/ULTIMA_COPIA_EXTERNA_OK"
    if [ ! -f "$marca" ] || [ "$(edad_horas "$marca")" -gt $((MAX_EDAD_HORAS + 4)) ]; then
        alertar "La copia EXTERNA no está al día" "La última subida exitosa fue hace más de $((MAX_EDAD_HORAS + 4)) h. Revisa $LOG_FILE y la conexión a internet."
        exit 1
    fi
else
    log "AVISO: no hay copia externa configurada; los respaldos viven solo en este equipo."
fi
log "OK: respaldo reciente ($(edad_horas "$ultimo_db") h), íntegro y completo."

if [ "$SOLO_FRESCURA" = 1 ]; then
    trap - ERR; log "--- Verificación (frescura) OK ---"; exit 0
fi

# ---------------------------------------------------------------- restauración real en una base temporal
exigir_pgpass
pg_conexion
TEMP_DB="sge_verificacion_$$"
if ! createdb "${PG_OPTS[@]}" "$TEMP_DB" 2>>"$LOG_FILE"; then
    alertar "No se pudo crear la base temporal de verificación" "Da permiso: sudo -u postgres psql -c \"ALTER USER $DB_USER CREATEDB;\""
    exit 1
fi
limpiar() { dropdb "${PG_OPTS[@]}" --if-exists "$TEMP_DB" >/dev/null 2>&1 || true; }
trap 'limpiar; alertar "La restauración de prueba FALLÓ" "El dump $ultimo_db no se pudo restaurar en una base limpia. Revisa $LOG_FILE."' ERR
trap limpiar EXIT

gunzip -c "$ultimo_db" | psql -v ON_ERROR_STOP=1 -q "${PG_OPTS[@]}" -d "$TEMP_DB" > /dev/null 2>>"$LOG_FILE"

problemas=""
for tabla in usuarios planes_estudio alumnos cargos pagos calificaciones documentos_alumno; do
    n_vivo=$(psql -At "${PG_OPTS[@]}" -d "$DB_NAME" -c "select count(*) from $tabla" 2>/dev/null || echo "?")
    n_resp=$(psql -At "${PG_OPTS[@]}" -d "$TEMP_DB" -c "select count(*) from $tabla" 2>/dev/null || echo "?")
    log "  $tabla: producción=$n_vivo, respaldo restaurado=$n_resp"
    # Producción puede tener MÁS filas (llegaron después del respaldo); lo grave es que el respaldo esté vacío o incompleto.
    if [ "$n_resp" = "?" ] || { [ "$n_vivo" != "?" ] && [ "$n_vivo" -gt 0 ] && [ "$n_resp" -eq 0 ]; }; then
        problemas="$problemas $tabla"
    fi
done
if [ -n "$problemas" ]; then
    alertar "El respaldo restaurado NO coincide con producción" "Tablas vacías o ausentes en el respaldo:$problemas"
    exit 1
fi
trap - ERR
log "--- Verificación completa OK: el respaldo se restauró en una base limpia y sus datos coinciden ---"
