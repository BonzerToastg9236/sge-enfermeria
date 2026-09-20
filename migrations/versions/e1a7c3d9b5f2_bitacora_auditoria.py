"""Tabla bitacora_auditoria (quién hizo qué acción sensible)

PROBLEMA (auditoría 2026-09-19): cancelar cargos, condonar recargos, otorgar
becas, cambiar precios/recargos y administrar cuentas no dejaban rastro de
quién lo hizo, cuándo ni con qué valor anterior. Solo agrega una tabla nueva:
`downgrade` la elimina sin tocar ningún otro dato.

Revision ID: e1a7c3d9b5f2
Revises: c4d5e6f7a8b9
Create Date: 2026-09-19

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e1a7c3d9b5f2'
down_revision = 'c4d5e6f7a8b9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'bitacora_auditoria',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('fecha', sa.DateTime(), nullable=False),
        sa.Column('usuario_fk', sa.Integer(), nullable=True),
        sa.Column('usuario_nombre', sa.String(length=150), nullable=True),
        sa.Column('accion', sa.String(length=40), nullable=False),
        sa.Column('entidad', sa.String(length=40), nullable=True),
        sa.Column('entidad_id', sa.String(length=40), nullable=True),
        sa.Column('matricula_fk', sa.String(length=20), nullable=True),
        sa.Column('detalle', sa.String(length=600), nullable=True),
        sa.ForeignKeyConstraint(['usuario_fk'], ['usuarios.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('bitacora_auditoria', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_bitacora_auditoria_accion'), ['accion'], unique=False)
        batch_op.create_index(batch_op.f('ix_bitacora_auditoria_fecha'), ['fecha'], unique=False)
        batch_op.create_index(batch_op.f('ix_bitacora_auditoria_matricula_fk'), ['matricula_fk'], unique=False)


def downgrade():
    with op.batch_alter_table('bitacora_auditoria', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_bitacora_auditoria_matricula_fk'))
        batch_op.drop_index(batch_op.f('ix_bitacora_auditoria_fecha'))
        batch_op.drop_index(batch_op.f('ix_bitacora_auditoria_accion'))
    op.drop_table('bitacora_auditoria')
