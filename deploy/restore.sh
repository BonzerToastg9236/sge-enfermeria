#!/bin/bash
set -euo pipefail

# ============================================================
# Restore script - Sistema de Gestión Escolar (SGE)
#
# Restaura un respaldo específico generado por backup.sh.
#
# USO:
#   ./restore.sh /ruta/db_2026-07-15_03-00-00.sql.gz /ruta/uploads_2026-07-15_03-00-00.tar.gz
#
# ⚠️  ESTO SOBREESCRIBE la base de datos y los documentos actuales del
#     servidor. Úsalo solo cuando de verdad necesites recuperar un
#     respaldo (o para PROBAR que tus respaldos sí funcionan — hazlo
#     de vez en cuando en un servidor de prueba, no solo confíes en
#     que "seguro sí sirven").
# ============================================================

if [ "$#" -ne 2 ]; then
    echo "Uso: ./restore.sh <db_backup.sql.gz> <uploads_backup.tar.gz>"
    echo "Ejemplo: ./restore.sh /home/sge/backups/db_2026-07-15_03-00-00.sql.gz /home/sge/backups/uploads_2026-07-15_03-00-00.tar.gz"
    exit 1
fi

DB_BACKUP="$1"
UPLOADS_BACKUP="$2"

# --- Configuración: AJUSTA estos valores a tu servidor real ---
APP_DIR="/home/sge/sge_enfermeria"
DB_NAME="sge_produccion"
DB_USER="sge_user"

if [ ! -f "$DB_BACKUP" ]; then
    echo "ERROR: no se encontró el archivo $DB_BACKUP"
    exit 1
fi
if [ ! -f "$UPLOADS_BACKUP" ]; then
    echo "ERROR: no se encontró el archivo $UPLOADS_BACKUP"
    exit 1
fi

echo "⚠️  Esto SOBREESCRIBIRÁ la base de datos '$DB_NAME' y los documentos actuales."
read -p "Escribe 'si' para confirmar que quieres continuar: " CONFIRMAR
if [ "$CONFIRMAR" != "si" ]; then
    echo "Cancelado. No se hizo ningún cambio."
    exit 0
fi

echo "Deteniendo la aplicación (systemctl stop sge)..."
sudo systemctl stop sge

echo "Restaurando base de datos desde $DB_BACKUP ..."
gunzip -c "$DB_BACKUP" | psql -U "$DB_USER" -h localhost "$DB_NAME"

echo "Restaurando documentos desde $UPLOADS_BACKUP ..."
rm -rf "$APP_DIR/static/uploads"
tar -xzf "$UPLOADS_BACKUP" -C "$APP_DIR/static"

echo "Reiniciando la aplicación (systemctl start sge)..."
sudo systemctl start sge

echo ""
echo "✅ Restauración completa. Verifica en el navegador que todo se vea bien"
echo "   antes de dar por terminado el proceso."
