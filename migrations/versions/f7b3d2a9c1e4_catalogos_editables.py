"""Materias archivables y formato de matrícula configurable

- materias.activa: permite ARCHIVAR una materia que ya tiene calificaciones o carga académica
  (borrarla rompería el historial). Las existentes quedan activas.
- configuracion_institucion.matricula_*: cada institución define el formato de sus matrículas
  nuevas. Los valores por defecto reproducen el formato actual (LEN2026-00001).
`downgrade` quita las columnas nuevas; no toca ningún otro dato.

Revision ID: f7b3d2a9c1e4
Revises: e1a7c3d9b5f2
Create Date: 2026-09-20

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f7b3d2a9c1e4'
down_revision = 'e1a7c3d9b5f2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('materias', schema=None) as batch_op:
        batch_op.add_column(sa.Column('activa', sa.Boolean(), nullable=False, server_default=sa.true()))

    with op.batch_alter_table('configuracion_institucion', schema=None) as batch_op:
        batch_op.add_column(sa.Column('matricula_prefijo', sa.String(length=6), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('matricula_incluye_clave', sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column('matricula_incluye_anio', sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column('matricula_separador', sa.String(length=2), nullable=False, server_default='-'))
        batch_op.add_column(sa.Column('matricula_digitos', sa.Integer(), nullable=False, server_default='5'))


def downgrade():
    with op.batch_alter_table('configuracion_institucion', schema=None) as batch_op:
        batch_op.drop_column('matricula_digitos')
        batch_op.drop_column('matricula_separador')
        batch_op.drop_column('matricula_incluye_anio')
        batch_op.drop_column('matricula_incluye_clave')
        batch_op.drop_column('matricula_prefijo')

    with op.batch_alter_table('materias', schema=None) as batch_op:
        batch_op.drop_column('activa')
