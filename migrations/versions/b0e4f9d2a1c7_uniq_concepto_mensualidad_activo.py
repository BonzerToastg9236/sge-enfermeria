"""Índice único parcial: un solo concepto de cobro "es mensualidad" activo

PROBLEMA: nuevo_cargo() (rutas/cobros.py) y _monto_mensualidad_con_beca()
(servicios/cobros.py) hacen
    ConceptoCobro.query.filter_by(es_mensualidad=True, activo=True).first()
para saber qué concepto usar como "la mensualidad" de la institución. Sin
ninguna restricción en la BD, nada impide que Dirección marque un SEGUNDO
concepto como es_mensualidad=True mientras el primero sigue activo -- con
dos así, cuál "gana" el .first() sería arbitrario según el orden físico
de la tabla, sin ningún aviso ni error.

LLAVE ÚNICA: a lo más UNA fila con (es_mensualidad = True AND activo = True)
a la vez. Es un índice único PARCIAL (no un UniqueConstraint normal) porque
la condición solo aplica a esas filas -- cualquier cantidad de conceptos
con es_mensualidad=False, o es_mensualidad=True pero activo=False (ej. una
mensualidad vieja que ya se dio de baja), pueden coexistir sin problema.

VERIFICADO ANTES DE CREAR EL ÍNDICE:
  SELECT COUNT(*) FROM conceptos_cobro WHERE es_mensualidad=1 AND activo=1
  -- sobre instance/sge_dev.db: 0 filas. No hay conflicto que bloquee esta
  -- migración en desarrollo. En el VPS de producción, correr esta misma
  -- consulta ANTES de "flask db upgrade" -- si ahí hay más de una fila,
  -- la migración fallará con un error de UNIQUE constraint al crear el
  -- índice, y hay que decidir a mano cuál de las dos es la vigente antes
  -- de reintentar.

Revision ID: b0e4f9d2a1c7
Revises: b7e2c9a41f3d
Create Date: 2026-09-15

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b0e4f9d2a1c7'
down_revision = 'b7e2c9a41f3d'
branch_labels = None
depends_on = None

NOMBRE_INDICE = 'uq_conceptos_cobro_una_mensualidad_activa'


def upgrade():
    op.create_index(
        NOMBRE_INDICE,
        'conceptos_cobro',
        ['es_mensualidad'],
        unique=True,
        sqlite_where=sa.text('es_mensualidad = 1 AND activo = 1'),
        postgresql_where=sa.text('es_mensualidad = true AND activo = true'),
    )


def downgrade():
    op.drop_index(NOMBRE_INDICE, table_name='conceptos_cobro')
