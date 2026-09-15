"""
Pruebas para el índice único parcial que garantiza un solo concepto
"es mensualidad" activo a la vez (hallazgo #5.2 de la auditoría, plan de
remediación paso 5.2).

CONTEXTO: nuevo_cargo() y _monto_mensualidad_con_beca() hacen
`ConceptoCobro.query.filter_by(es_mensualidad=True, activo=True).first()`
-- con dos conceptos así marcados a la vez, cuál "gana" sería arbitrario
según el orden físico de la tabla, sin ningún aviso. El índice único
parcial lo impide a nivel de BD; las 3 vistas que pueden intentarlo
(alta, edición de precio, activar/desactivar) deben atrapar el
IntegrityError con un mensaje claro en vez de tronar con un 500.

Mismo patrón de prueba que tests/test_cargos_duplicados.py.
"""

from sqlalchemy.exc import IntegrityError

from tests.conftest import crear_usuario, login
from tests.test_cargos_duplicados import _crear_concepto

from app import db, ConceptoCobro


# ---------------------------------------------------------------------------
# Nivel BD: el índice único parcial en sí
# ---------------------------------------------------------------------------

def test_indice_unico_rechaza_dos_conceptos_mensualidad_activos_a_la_vez(app):
    _crear_concepto('Colegiatura')
    concepto_1 = ConceptoCobro.query.filter_by(nombre='Colegiatura').first()
    concepto_1.es_mensualidad = True
    db.session.commit()

    concepto_2 = ConceptoCobro(nombre='Mensualidad Alterna', es_mensualidad=True, activo=True)
    db.session.add(concepto_2)
    try:
        db.session.commit()
        assert False, 'La BD debió rechazar un segundo concepto es_mensualidad=True activo'
    except IntegrityError:
        db.session.rollback()

    assert ConceptoCobro.query.filter_by(es_mensualidad=True, activo=True).count() == 1


def test_permite_un_segundo_concepto_mensualidad_si_el_primero_esta_inactivo(app):
    concepto_1 = ConceptoCobro(nombre='Colegiatura Vieja', es_mensualidad=True, activo=False)
    db.session.add(concepto_1)
    db.session.commit()

    concepto_2 = ConceptoCobro(nombre='Colegiatura Nueva', es_mensualidad=True, activo=True)
    db.session.add(concepto_2)
    db.session.commit()  # no debe chocar: el primero ya no cuenta

    assert ConceptoCobro.query.filter_by(es_mensualidad=True, activo=True).count() == 1


def test_permite_varios_conceptos_activos_que_no_son_mensualidad(app):
    db.session.add(ConceptoCobro(nombre='Inscripción', es_mensualidad=False, activo=True))
    db.session.add(ConceptoCobro(nombre='Uniformes', es_mensualidad=False, activo=True))
    db.session.commit()  # no debe chocar: ninguno es es_mensualidad

    assert ConceptoCobro.query.filter_by(activo=True).count() == 2


# ---------------------------------------------------------------------------
# Nivel HTTP: las 3 vistas que pueden intentar violar el índice deben
# atraparlo con un mensaje claro, nunca un 500.
# ---------------------------------------------------------------------------

def test_alta_de_concepto_mensualidad_duplicado_no_truena_con_500(client, app):
    _crear_concepto('Colegiatura')
    concepto_1 = ConceptoCobro.query.filter_by(nombre='Colegiatura').first()
    concepto_1.es_mensualidad = True
    db.session.commit()
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    respuesta = client.post(
        '/conceptos-cobro',
        data={'nombre': 'Colegiatura Alterna', 'es_mensualidad': 'on'},
        follow_redirects=True,
    )

    assert respuesta.status_code == 200
    assert ConceptoCobro.query.filter_by(nombre='Colegiatura Alterna').first() is None
    assert ConceptoCobro.query.filter_by(es_mensualidad=True, activo=True).count() == 1


def test_editar_precio_no_puede_marcar_una_segunda_mensualidad_activa(client, app):
    _crear_concepto('Colegiatura')
    concepto_1 = ConceptoCobro.query.filter_by(nombre='Colegiatura').first()
    concepto_1.es_mensualidad = True
    db.session.commit()

    concepto_2 = _crear_concepto('Cuota Especial')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    respuesta = client.post(
        f'/conceptos-cobro/{concepto_2.id}/editar-precio',
        data={'monto_sugerido': '', 'es_mensualidad': 'on'},
        follow_redirects=True,
    )

    assert respuesta.status_code == 200
    db.session.refresh(concepto_2)
    assert concepto_2.es_mensualidad is False, 'No debió quedar marcado como mensualidad'
    assert ConceptoCobro.query.filter_by(es_mensualidad=True, activo=True).count() == 1


def test_activar_un_concepto_mensualidad_inactivo_no_puede_duplicar_el_activo(client, app):
    _crear_concepto('Colegiatura')
    concepto_1 = ConceptoCobro.query.filter_by(nombre='Colegiatura').first()
    concepto_1.es_mensualidad = True
    db.session.commit()

    concepto_2 = ConceptoCobro(nombre='Colegiatura Congelada', es_mensualidad=True, activo=False)
    db.session.add(concepto_2)
    db.session.commit()
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    respuesta = client.post(f'/conceptos-cobro/{concepto_2.id}/toggle', follow_redirects=True)

    assert respuesta.status_code == 200
    db.session.refresh(concepto_2)
    assert concepto_2.activo is False, 'No debió activarse: dejaría dos mensualidades activas'
    assert ConceptoCobro.query.filter_by(es_mensualidad=True, activo=True).count() == 1
