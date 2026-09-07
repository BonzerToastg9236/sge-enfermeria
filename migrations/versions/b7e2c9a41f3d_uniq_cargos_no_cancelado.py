"""Índice único parcial contra cargos duplicados (check-then-insert sin respaldo en BD)

PROBLEMA: _cargo_duplicado() en app.py hace un SELECT en Python antes de
INSERTAR un Cargo, para no cobrarle dos veces el mismo concepto al mismo
alumno en el mismo periodo. Sin una restricción real en la BD, dos
peticiones concurrentes (doble clic, o dos operadores corriendo
generar_mensualidades()/avanzar_cuatrimestre_lote() casi al mismo tiempo)
pueden pasar ambas el SELECT antes de que cualquiera haga su INSERT, y
terminar con dos cargos duplicados.

LLAVE ÚNICA (tomada literalmente de _cargo_duplicado(), no inventada):
    (matricula_fk, concepto_cobro_fk, periodo_escolar)
    -- excluyendo cargos con estatus CANCELADO (_cargo_duplicado() los
       ignora a propósito: cancelar y volver a cobrar el mismo concepto
       en el mismo periodo es un flujo legítimo, ver cancelar_cargo()).

POR QUÉ ES UN ÍNDICE PARCIAL CON EXPRESIÓN Y NO UN UniqueConstraint SIMPLE:
  1. La exclusión de CANCELADO no se puede expresar con UniqueConstraint
     (no acepta condición WHERE) -- se necesita un índice único PARCIAL.
  2. periodo_escolar es NULLABLE (hay cargos de un solo pago sin periodo,
     ej. "Uniformes", ver nuevo_cargo() en app.py). En SQL estándar, NULL
     nunca es igual a otro NULL dentro de una restricción UNIQUE -- dos
     cargos con periodo_escolar NULL NO chocarían con un UniqueConstraint
     normal, aunque _cargo_duplicado() SÍ los trata como duplicados
     (`Cargo.periodo_escolar == periodo_escolar` con periodo_escolar=None
     genera "periodo_escolar IS NULL", que sí empareja NULL con NULL).
     Por eso se usa COALESCE(periodo_escolar, '') en la expresión del
     índice: así dos cargos con periodo_escolar NULL sí se consideran la
     misma llave, igual que ya hace el código Python. ('' es un valor
     imposible para periodo_escolar real: request.form.get(...).strip()
     or None convierte cualquier cadena vacía en None antes de guardarse.)

VERIFICADO ANTES DE CREAR EL ÍNDICE (no se asume, se comprobó de verdad):
  SELECT matricula_fk, concepto_cobro_fk, periodo_escolar, COUNT(*)
  FROM cargos WHERE estatus != 'CANCELADO'
  GROUP BY matricula_fk, concepto_cobro_fk, periodo_escolar HAVING COUNT(*) > 1
  -- sobre instance/sge_dev.db: 0 filas. No hay duplicados que bloqueen
  -- esta migración en desarrollo. En el VPS de producción, correr esta
  -- misma consulta ANTES de "flask db upgrade" -- si ahí sí hay
  -- duplicados, la migración fallará con un error de UNIQUE constraint
  -- al crear el índice, y hay que decidir a mano (nunca automáticamente)
  -- qué hacer con cada caso antes de reintentar.

Revision ID: b7e2c9a41f3d
Revises: f1a2b3c4d5e6
Create Date: 2026-09-06

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7e2c9a41f3d'
down_revision = 'f1a2b3c4d5e6'
branch_labels = None
depends_on = None

NOMBRE_INDICE = 'uq_cargos_activo_matricula_concepto_periodo'
CONDICION_WHERE = "estatus != 'CANCELADO'"


def upgrade():
    op.create_index(
        NOMBRE_INDICE,
        'cargos',
        ['matricula_fk', 'concepto_cobro_fk', sa.text("COALESCE(periodo_escolar, '')")],
        unique=True,
        sqlite_where=sa.text(CONDICION_WHERE),
        postgresql_where=sa.text(CONDICION_WHERE),
    )


def downgrade():
    op.drop_index(NOMBRE_INDICE, table_name='cargos')
