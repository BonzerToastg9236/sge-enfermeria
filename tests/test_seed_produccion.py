"""
Despliegue 2026-09-20: `python seed.py` sembraba 4 planes de DEMOSTRACIÓN
(Enfermería + 3 ingenierías con materias de ejemplo), que los aspirantes
verían en /registro, y dejaba el concepto "Colegiatura" sin marcar como
mensualidad: sin ese marcador la generación automática de mensualidades no
hace nada y no avisa. `python seed.py --produccion` deja solo lo necesario.
"""

from app import db, PlanEstudio, Materia, ConceptoCobro
from seed import sembrar


def test_el_modo_produccion_siembra_solo_enfermeria_y_sin_materias_de_ejemplo(app):
    sembrar(produccion=True)
    assert [p.clave_carrera for p in PlanEstudio.query.all()] == ['LEN']
    assert Materia.query.count() == 0          # el plan de estudios oficial lo carga la institución


def test_la_colegiatura_queda_marcada_como_mensualidad_en_ambos_modos(app):
    sembrar(produccion=True)
    colegiatura = ConceptoCobro.query.filter_by(nombre='Colegiatura').one()
    assert colegiatura.es_mensualidad is True and colegiatura.monto_sugerido is None   # el precio lo captura Dirección


def test_el_modo_demo_conserva_los_planes_de_ejemplo(app):
    sembrar()
    assert {p.clave_carrera for p in PlanEstudio.query.all()} == {'LEN', 'ISC', 'IIN', 'ICI'}
    assert Materia.query.count() > 0


def test_sembrar_dos_veces_no_duplica_nada(app):
    sembrar(produccion=True); sembrar(produccion=True)
    assert PlanEstudio.query.count() == 1 and ConceptoCobro.query.filter_by(nombre='Colegiatura').count() == 1
