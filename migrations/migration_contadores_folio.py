"""agrega tabla contadores_folio

Revision ID: <reemplazar_con_el_id_que_genere_alembic>
Revises: <reemplazar_con_el_ultimo_head_actual>
Create Date: 2026-07-25

INSTRUCCIONES PARA INTEGRAR ESTO A TU PROYECTO:

1. Detén `python app.py` y revisa que no haya ningún proceso python huérfano
   corriendo (Get-Process python en Windows) -- disciplina ya documentada en
   el proyecto, sigue aplicando aquí igual que en cualquier otra migración.

2. Genera el esqueleto real con Alembic (para que tome el head correcto):
       flask db migrate -m "agrega tabla contadores_folio"

3. Alembic va a intentar autogenerar el `upgrade()`/`downgrade()` a partir
   de los modelos -- compáralo contra lo de abajo. Si el nombre de la
   restricción única que genera automáticamente no es explícito
   ('uq_contador_folio_tipo_anio'), reemplázalo a mano por el de aquí.
   Recuerda: en SQLite toda restricción necesita nombre explícito para que
   el modo batch pueda recrear la tabla (lección ya documentada).

4. Revisa el archivo generado y reemplaza su contenido por este si
   coincide, o ajústalo si Alembic detectó algo distinto en tu esquema
   actual.

5. Aplica con: flask db upgrade
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '<reemplazar>'
down_revision = '<reemplazar>'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'contadores_folio',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tipo', sa.String(length=30), nullable=False),
        sa.Column('anio', sa.Integer(), nullable=False),
        sa.Column('ultimo_valor', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id', name='pk_contadores_folio'),
        sa.UniqueConstraint('tipo', 'anio', name='uq_contador_folio_tipo_anio'),
    )

    # --- Poblar el contador leyendo las actas YA emitidas, por año ---
    # No asumimos que empieza en 0: si ya hay actas capturadas (aunque sea
    # en desarrollo, con datos de prueba), hay que sembrar cada (tipo, año)
    # con el número más alto ya usado, para no reutilizar un folio que ya
    # existe. Esto sí se ejecuta con Python normal (no SQL crudo dependiente
    # del motor) para que funcione igual en SQLite y en PostgreSQL.
    conn = op.get_bind()
    filas = conn.execute(
        sa.text("SELECT numero_acta FROM calificaciones WHERE numero_acta LIKE 'ACTA-%'")
    ).fetchall()

    maximos_por_anio = {}
    for (numero_acta,) in filas:
        try:
            _, anio_str, consecutivo_str = numero_acta.split('-')
            anio = int(anio_str)
            consecutivo = int(consecutivo_str)
        except (ValueError, AttributeError):
            continue  # numero_acta con formato viejo/inesperado: se ignora, no rompe la migración
        maximos_por_anio[anio] = max(maximos_por_anio.get(anio, 0), consecutivo)

    contadores_folio = sa.table(
        'contadores_folio',
        sa.column('tipo', sa.String),
        sa.column('anio', sa.Integer),
        sa.column('ultimo_valor', sa.Integer),
    )
    if maximos_por_anio:
        op.bulk_insert(contadores_folio, [
            {'tipo': 'ACTA', 'anio': anio, 'ultimo_valor': valor}
            for anio, valor in maximos_por_anio.items()
        ])
    # Si no hay ninguna acta todavía, no se inserta ninguna fila -- eso es
    # correcto: siguiente_folio() crea la primera fila sola, en 0, la
    # primera vez que se pida un folio de un (tipo, año) nuevo.


def downgrade():
    op.drop_table('contadores_folio')
