#!/bin/bash
set -euo pipefail

# ============================================================
# Restore script - Sistema de Gestión Escolar (SGE)
#
# Restaura un respaldo específico generado por backup.sh.
#
# USO:
#   ./restore.sh /ruta/db_2026-07-15_03-00-00.sql.gz /ruta/documentos_2026-07-15_03-00-00.tar.gz
#
# ⚠️  ESTO SOBREESCRIBE la base de datos y los documentos actuales del
#     servidor. Úsalo solo cuando de verdad necesites recuperar un
#     respaldo (o para PROBAR que tus respaldos sí funcionan — hazlo
#     de vez en cuando en un servidor de prueba, no solo confíes en
#     que "seguro sí sirven").
#
# ------------------------------------------------------------
# CAMBIOS DE LA AUDITORÍA (no revertir sin leer esto):
#
# [D3] psql -v ON_ERROR_STOP=1. Por defecto, psql CONTINÚA después de un
#      error y termina devolviendo "éxito". Con la versión anterior de
#      este script, una restauración que falló a la mitad imprimía
#      igualmente "✅ Restauración completa" -- el peor mensaje posible,
#      porque te hace creer que recuperaste todo cuando no fue así.
#
# [D4] VALIDAR ANTES DE DESTRUIR. La versión anterior hacía
#      "rm -rf documentos_alumnos" y DESPUÉS intentaba extraer el .tar.gz.
#      Si el archivo estaba corrupto o era el equivocado, ya habías
#      borrado las INEs, actas y certificados escaneados, sin vuelta
#      atrás. Ahora: primero se valida que ambos respaldos se puedan
#      leer completos, y la carpeta vieja se CONSERVA renombrada hasta
#      confirmar que la nueva quedó bien.
# ============================================================

if [ "$#" -ne 2 ]; then
    echo "Uso: ./restore.sh <db_backup.sql.gz> <documentos_backup.tar.gz>"
    echo "Ejemplo: ./restore.sh /home/sge/backups/db_2026-07-15_03-00-00.sql.gz /home/sge/backups/documentos_2026-07-15_03-00-00.tar.gz"
    exit 1
fi

DB_BACKUP="$1"
UPLOADS_BACKUP="$2"

# --- Configuración: AJUSTA estos valores a tu servidor real ---
APP_DIR="/home/sge/sge_enfermeria"
DB_NAME="sge_produccion"
DB_USER="sge_user"

export PGPASSFILE="/home/sge/.pgpass"

FECHA=$(date +%Y-%m-%d_%H-%M-%S)

# ------------------------------------------------------------
# FASE 1 - VALIDACIÓN (todavía no se toca nada del servidor) [D4]
# ------------------------------------------------------------
echo "Validando los archivos de respaldo antes de tocar nada..."

if [ ! -f "$DB_BACKUP" ]; then
    echo "ERROR: no se encontró el archivo $DB_BACKUP"
    exit 1
fi
if [ ! -f "$UPLOADS_BACKUP" ]; then
    echo "ERROR: no se encontró el archivo $UPLOADS_BACKUP"
    exit 1
fi

if ! gzip -t "$DB_BACKUP" 2>/dev/null; then
    echo "ERROR: el respaldo de base de datos está CORRUPTO (no se puede descomprimir)."
    echo "       No se hizo ningún cambio. Prueba con otro respaldo más antiguo."
    exit 1
fi
echo "  ✓ Respaldo de base de datos íntegro"

if ! tar -tzf "$UPLOADS_BACKUP" > /dev/null 2>&1; then
    echo "ERROR: el respaldo de documentos está CORRUPTO (no se puede leer el .tar.gz)."
    echo "       No se hizo ningún cambio. Prueba con otro respaldo más antiguo."
    exit 1
fi
echo "  ✓ Respaldo de documentos íntegro"

if [ ! -f "$PGPASSFILE" ]; then
    echo "ERROR: no existe $PGPASSFILE — psql no podrá autenticarse."
    echo "       Créalo con:  echo 'localhost:5432:$DB_NAME:$DB_USER:TU_PASSWORD' > ~/.pgpass && chmod 600 ~/.pgpass"
    exit 1
fi
echo "  ✓ Credenciales de PostgreSQL disponibles"

# ------------------------------------------------------------
# FASE 2 - CONFIRMACIÓN EXPLÍCITA
# ------------------------------------------------------------
echo ""
echo "⚠️  Esto SOBREESCRIBIRÁ la base de datos '$DB_NAME' y los documentos actuales."
echo "    Base de datos: $DB_BACKUP"
echo "    Documentos:    $UPLOADS_BACKUP"
echo ""
read -p "Escribe 'si' para confirmar que quieres continuar: " CONFIRMAR
if [ "$CONFIRMAR" != "si" ]; then
    echo "Cancelado. No se hizo ningún cambio."
    exit 0
fi

# ------------------------------------------------------------
# FASE 3 - RESTAURACIÓN
# ------------------------------------------------------------
echo "Deteniendo la aplicación (systemctl stop sge)..."
sudo systemctl stop sge

echo "Restaurando base de datos desde $DB_BACKUP ..."
# [D3] ON_ERROR_STOP=1: si algo falla, psql se detiene y devuelve error,
# en vez de seguir adelante y reportar éxito sobre una restauración rota.
# El dump se genera con --clean --if-exists (ver backup.sh), así que las
# tablas existentes se eliminan correctamente antes de recrearse.
if ! gunzip -c "$DB_BACKUP" | psql -v ON_ERROR_STOP=1 -U "$DB_USER" -h localhost "$DB_NAME"; then
    echo ""
    echo "❌ ERROR: la restauración de la base de datos FALLÓ."
    echo "   La base puede haber quedado en un estado incompleto."
    echo "   Los documentos NO se tocaron todavía."
    echo "   La aplicación sigue detenida a propósito — revisa el error de arriba"
    echo "   antes de volver a levantarla con: sudo systemctl start sge"
    exit 1
fi
echo "  ✓ Base de datos restaurada"

echo "Restaurando documentos desde $UPLOADS_BACKUP ..."
DOCS_DIR="$APP_DIR/instance/documentos_alumnos"
DOCS_RESPALDO="$APP_DIR/instance/documentos_alumnos.anterior_$FECHA"

# [D4] La carpeta actual se RENOMBRA, no se borra. Si la extracción falla,
# todavía podemos devolverla a su lugar.
if [ -d "$DOCS_DIR" ]; then
    mv "$DOCS_DIR" "$DOCS_RESPALDO"
    echo "  ✓ Documentos actuales preservados en: $DOCS_RESPALDO"
fi

if tar -xzf "$UPLOADS_BACKUP" -C "$APP_DIR/instance"; then
    echo "  ✓ Documentos restaurados"
else
    echo "❌ ERROR: falló la extracción de los documentos."
    if [ -d "$DOCS_RESPALDO" ]; then
        rm -rf "$DOCS_DIR"
        mv "$DOCS_RESPALDO" "$DOCS_DIR"
        echo "   Se restauraron los documentos que había antes (nada se perdió)."
    fi
    echo "   La aplicación sigue detenida — revisa el error antes de continuar."
    exit 1
fi

echo "Reiniciando la aplicación (systemctl start sge)..."
sudo systemctl start sge

echo ""
echo "✅ Restauración completa. Verifica en el navegador que todo se vea bien"
echo "   antes de dar por terminado el proceso."
echo ""
if [ -d "$DOCS_RESPALDO" ]; then
    echo "📁 Los documentos que existían ANTES de esta restauración siguen guardados en:"
    echo "     $DOCS_RESPALDO"
    echo "   Bórralos a mano SOLO cuando hayas confirmado que todo quedó correcto:"
    echo "     rm -rf $DOCS_RESPALDO"
fi
