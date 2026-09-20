"""Nombra las restricciones únicas de conceptos_cobro.nombre y pagos.folio

Estas dos restricciones se crearon sin nombre explícito en la migración
inicial (0cc9815005a8). En SQLite, una restricción UNIQUE sin nombre no
se puede referenciar para modificarla más adelante con batch_alter_table
(ej. si algún día hay que quitarla o cambiar sus columnas) -- hay que
apoyarse en una naming_convention temporal para que Alembic pueda
"verla" y reemplazarla por una con nombre real.

Revision ID: f1a2b3c4d5e6
Revises: a3ffe23d5c99
Create Date: 2026-09-05

"""
import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = 'f1a2b3c4d5e6'
down_revision = 'a3ffe23d5c99'
branch_labels = None
depends_on = None

# Convención temporal SOLO para que Alembic pueda referirse a la
# restricción sin nombre ya existente al reflejar la tabla -- no cambia
# nada por sí sola, es la clave para poder hacer drop_constraint() de algo
# que nunca tuvo nombre.
NAMING_CONVENTION = {'uq': 'uq_%(table_name)s_%(column_0_name)s'}


# (tabla, columna, nombre final) de las restricciones UNIQUE que se nombran aquí.
RESTRICCIONES = [
    ('conceptos_cobro', 'nombre', 'uq_conceptos_cobro_nombre'),
    ('pagos', 'folio', 'uq_pagos_folio'),
]


def upgrade():
    if op.get_bind().dialect.name == 'sqlite':
        _nombrar_en_sqlite()
    else:
        _nombrar_en_postgresql()


def _nombrar_en_sqlite():
    with op.batch_alter_table('conceptos_cobro', schema=None, naming_convention=NAMING_CONVENTION) as batch_op:
        batch_op.drop_constraint('uq_conceptos_cobro_nombre', type_='unique')
        batch_op.create_unique_constraint('uq_conceptos_cobro_nombre', ['nombre'])

    with op.batch_alter_table('pagos', schema=None, naming_convention=NAMING_CONVENTION) as batch_op:
        batch_op.drop_constraint('uq_pagos_folio', type_='unique')
        batch_op.create_unique_constraint('uq_pagos_folio', ['folio'])


def _nombrar_en_postgresql():
    """
    PostgreSQL SÍ le puso nombre a esas restricciones al crearlas la migración
    inicial ("conceptos_cobro_nombre_key", "pagos_folio_key"), así que no hay
    nada que soltar y recrear (el DROP de arriba fallaba con "constraint ...
    does not exist" y `flask db upgrade` no terminaba en un servidor nuevo):
    basta RENOMBRARLAS, sin tocar datos ni perder la unicidad ni un instante.
    """
    inspector = sa.inspect(op.get_bind())
    for tabla, columna, nombre_final in RESTRICCIONES:
        existentes = inspector.get_unique_constraints(tabla)
        if any(u['name'] == nombre_final for u in existentes):
            continue  # ya tiene el nombre esperado
        actual = next(u['name'] for u in existentes if u['column_names'] == [columna])
        op.execute(f'ALTER TABLE {tabla} RENAME CONSTRAINT "{actual}" TO "{nombre_final}"')


def downgrade():
    # No-op intencional: Alembic exige un nombre para crear una restricción
    # UNIQUE (create_unique_constraint no acepta name=None), así que no hay
    # forma de "devolverle" el anonimato original vía la API de batch mode.
    # Como ponerle nombre a una restricción ya existente no cambia ningún
    # comportamiento (solo la hace referenciable en migraciones futuras),
    # dejarla con nombre al bajar esta migración es inocuo.
    pass
