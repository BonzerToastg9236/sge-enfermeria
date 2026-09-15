"""CheckConstraint: alumnos.cuatrimestre_actual siempre >= 1

PROBLEMA: la importación masiva (rutas/alumnos.py::importar_alumnos)
solo validaba que cuatrimestre_actual fuera un número, sin acotar su
rango -- un 0 o negativo en el archivo .xlsx se guardaba tal cual, sin
ningún aviso. Mismo patrón que ck_materia_cuatrimestre_positivo
(migración 0cc9815005a8) para Materia.cuatrimestre, que ya existía.

El tope SUPERIOR (_max_periodos(), configurable por institución vía
ConfiguracionInstitucion.max_periodos) no se puede expresar aquí --
un CheckConstraint es una expresión fija del esquema, no puede leer
otra tabla. Ese tope se valida en la aplicación (rutas/alumnos.py);
este CheckConstraint solo es el piso, igual que en Materia.

VERIFICADO ANTES DE CREAR EL ÍNDICE:
  SELECT COUNT(*) FROM alumnos WHERE cuatrimestre_actual < 1
  -- sobre instance/sge_dev.db: 0 filas.

Revision ID: c4d5e6f7a8b9
Revises: b0e4f9d2a1c7
Create Date: 2026-09-15

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = 'c4d5e6f7a8b9'
down_revision = 'b0e4f9d2a1c7'
branch_labels = None
depends_on = None

NOMBRE_CONSTRAINT = 'ck_alumno_cuatrimestre_positivo'
CONDICION = 'cuatrimestre_actual >= 1'


def upgrade():
    with op.batch_alter_table('alumnos', schema=None) as batch_op:
        batch_op.create_check_constraint(NOMBRE_CONSTRAINT, CONDICION)


def downgrade():
    with op.batch_alter_table('alumnos', schema=None) as batch_op:
        batch_op.drop_constraint(NOMBRE_CONSTRAINT, type_='check')
