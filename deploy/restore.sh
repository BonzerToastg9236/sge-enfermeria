#!/bin/bash
# =============================================================================
# Restaura un respaldo del SGE (base de datos + documentos).
#
#   ./restore.sh <db_*.sql.gz[.gpg]> <documentos_*.tar.gz[.gpg]>
#   ./restore.sh <db_*.sql.gz[.gpg]> --sin-documentos
#
# Sirve igual para un respaldo LOCAL que para la copia EXTERNA CIFRADA (.gpg) bajada de la nube en un
# servidor nuevo: los archivos .gpg se descifran con la frase de $BACKUP_PASSPHRASE_FILE.
#
# Qué garantiza:
#   * valida TODO antes de tocar nada (íntegros, dump completo);
#   * ANTES de sobrescribir guarda un respaldo de seguridad de lo que hubiera (restauración reversible);
#   * la base se restaura con ON_ERROR_STOP (no deja "éxitos" a medias) y luego corre `flask db upgrade`
#     por si el respaldo es de una versión anterior del esquema;
#   * los documentos actuales NO se borran: quedan renombrados hasta que confirmes que todo está bien.
# =============================================================================
set -euo pipefail
umask 077
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib_respaldo.sh"

if [ "$#" -ne 2 ]; then
    echo "Uso: ./restore.sh <db_*.sql.gz[.gpg]> <documentos_*.tar.gz[.gpg] | --sin-documentos>"
    exit 1
fi
DB_BACKUP="$1"; UPLOADS_BACKUP="$2"
SIN_SERVICIO="${SIN_SERVICIO:-0}"          # 1 = no detener/arrancar el servicio systemd (pruebas)
SERVICIO="${SERVICIO:-sge}"
FLASK_BIN="${FLASK_BIN:-$APP_DIR/venv/bin/flask}"
FECHA=$(date +%Y-%m-%d_%H-%M-%S)
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

descifrar_si_hace_falta() {   # $1 = archivo ; imprime la ruta del archivo utilizable
    case "$1" in
        *.gpg)
            [ -s "$BACKUP_PASSPHRASE_FILE" ] || { echo "ERROR: falta la frase de contraseña ($BACKUP_PASSPHRASE_FILE) para descifrar $1" >&2; exit 1; }
            local salida="$TMP/$(basename "${1%.gpg}")"
            gpg --batch --yes --quiet --decrypt --passphrase-file "$BACKUP_PASSPHRASE_FILE" -o "$salida" "$1"
            echo "$salida" ;;
        *) echo "$1" ;;
    esac
}

echo "Validando los archivos de respaldo antes de tocar nada..."
[ -f "$DB_BACKUP" ] || { echo "ERROR: no se encontró $DB_BACKUP"; exit 1; }
DB_USABLE="$(descifrar_si_hace_falta "$DB_BACKUP")"
if ! gzip -t "$DB_USABLE" 2>/dev/null || ! gunzip -c "$DB_USABLE" | tail -n 5 | grep -q "PostgreSQL database dump complete"; then
    echo "ERROR: el respaldo de la base está CORRUPTO o incompleto. No se hizo ningún cambio. Prueba con uno más antiguo."; exit 1
fi
echo "  ✓ Respaldo de base de datos íntegro y completo"

DOCS_USABLE=""
if [ "$UPLOADS_BACKUP" != "--sin-documentos" ]; then
    [ -f "$UPLOADS_BACKUP" ] || { echo "ERROR: no se encontró $UPLOADS_BACKUP"; exit 1; }
    DOCS_USABLE="$(descifrar_si_hace_falta "$UPLOADS_BACKUP")"
    tar -tzf "$DOCS_USABLE" > /dev/null 2>&1 || { echo "ERROR: el respaldo de documentos está CORRUPTO. No se hizo ningún cambio."; exit 1; }
    echo "  ✓ Respaldo de documentos íntegro"
fi
exigir_pgpass
pg_conexion
echo "  ✓ Credenciales de PostgreSQL disponibles"

echo ""
echo "⚠️  Esto SOBREESCRIBIRÁ la base '$DB_NAME'${DOCS_USABLE:+ y los documentos actuales}."
echo "    Base de datos: $DB_BACKUP"
[ -n "$DOCS_USABLE" ] && echo "    Documentos:    $UPLOADS_BACKUP"
echo "    (Antes de sobrescribir se guarda una copia de lo que haya ahora en $BACKUP_DIR)"
echo ""
read -r -p "Escribe 'si' para confirmar que quieres continuar: " CONFIRMAR
[ "$CONFIRMAR" = "si" ] || { echo "Cancelado. No se hizo ningún cambio."; exit 0; }

if [ "$SIN_SERVICIO" != "1" ]; then
    echo "Deteniendo la aplicación (systemctl stop $SERVICIO)..."
    sudo systemctl stop "$SERVICIO"
fi

# La base debe existir (en un servidor nuevo, créala como en DEPLOYMENT.md paso 3; aquí se intenta si falta).
if ! psql -At "${PG_OPTS[@]}" -d "$DB_NAME" -c 'select 1' >/dev/null 2>&1; then
    createdb "${PG_OPTS[@]}" "$DB_NAME" || { echo "ERROR: la base $DB_NAME no existe y no se pudo crear. Créala (DEPLOYMENT.md, paso 3) y repite."; exit 1; }
    echo "  ✓ Base $DB_NAME creada"
else
    SEGURIDAD="$BACKUP_DIR/antes_de_restaurar_${FECHA}.sql.gz"
    pg_dump --clean --if-exists --no-owner --no-privileges "${PG_OPTS[@]}" "$DB_NAME" | gzip > "$SEGURIDAD"
    echo "  ✓ Copia de seguridad de lo actual: $SEGURIDAD"
fi

echo "Restaurando base de datos ..."
if ! gunzip -c "$DB_USABLE" | psql -v ON_ERROR_STOP=1 -q "${PG_OPTS[@]}" -d "$DB_NAME" > /dev/null; then
    echo ""
    echo "❌ ERROR: la restauración de la base de datos FALLÓ. La base pudo quedar incompleta."
    [ -n "${SEGURIDAD:-}" ] && echo "   Lo que había antes está en: $SEGURIDAD"
    echo "   Los documentos NO se tocaron. La aplicación sigue detenida a propósito."
    exit 1
fi
echo "  ✓ Base de datos restaurada"

if [ -x "$FLASK_BIN" ]; then
    echo "Actualizando el esquema por si el respaldo es de una versión anterior (flask db upgrade)..."
    (cd "$APP_DIR" && FLASK_APP=app.py "$FLASK_BIN" db upgrade > /dev/null 2>&1) && echo "  ✓ Esquema al día" || echo "  ⚠️  flask db upgrade no se pudo ejecutar: hazlo a mano."
fi

if [ -n "$DOCS_USABLE" ]; then
    DOCS_DIR="$APP_DIR/instance/documentos_alumnos"
    DOCS_ANTERIOR="$APP_DIR/instance/documentos_alumnos.anterior_$FECHA"
    mkdir -p "$APP_DIR/instance"
    if [ -d "$DOCS_DIR" ]; then
        if [ -n "$(ls -A "$DOCS_DIR" 2>/dev/null)" ]; then
            mv "$DOCS_DIR" "$DOCS_ANTERIOR" && echo "  ✓ Documentos actuales preservados en: $DOCS_ANTERIOR"
        else
            rmdir "$DOCS_DIR"      # vacía (la app la recrea al arrancar): no hay nada que preservar
        fi
    fi
    if tar -xzf "$DOCS_USABLE" -C "$APP_DIR/instance"; then
        echo "  ✓ Documentos restaurados"
    else
        echo "❌ ERROR: falló la extracción de los documentos."
        if [ -d "${DOCS_ANTERIOR:-/nonexistent}" ]; then rm -rf "$DOCS_DIR"; mv "$DOCS_ANTERIOR" "$DOCS_DIR"; echo "   Se recuperaron los documentos que había antes."; fi
        exit 1
    fi
fi

if [ "$SIN_SERVICIO" != "1" ]; then
    echo "Reiniciando la aplicación (systemctl start $SERVICIO)..."
    sudo systemctl start "$SERVICIO"
fi
echo ""
echo "✅ Restauración completa. Entra al sistema y verifica alumnos, cobros y un documento antes de dar por terminado el proceso."
[ -n "${DOCS_ANTERIOR:-}" ] && [ -d "$DOCS_ANTERIOR" ] && echo "📁 Documentos previos guardados en $DOCS_ANTERIOR (bórralos SOLO cuando confirmes que todo quedó bien)."
[ -n "${SEGURIDAD:-}" ] && echo "📁 Copia de la base previa: $SEGURIDAD"
