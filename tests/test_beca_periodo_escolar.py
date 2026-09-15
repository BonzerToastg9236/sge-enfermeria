"""
Pruebas de validación del periodo escolar al otorgar una beca (hallazgo
#5.3 de la auditoría, plan de remediación paso 5.3).

CONTEXTO: cobros.becas_alumno() usa
    Cargo.periodo_escolar.like(f'{periodo_escolar}%')
para ajustar cargos de mensualidad ya generados -- si periodo_escolar es
solo un año ("2026", sin guion), esto empareja TODOS los periodos de ese
año (2026-A, 2026-B, 2026-C), no uno solo. Es un comportamiento
INTENCIONAL para becas anuales, pero hasta ahora aplicaba en silencio:
nada impedía capturar texto arbitrario, y nada avisaba que "2026" afecta
a todo el año.

Dos correcciones:
1. Validar el formato (^\\d{4}(-[A-Z])?$) para rechazar texto arbitrario.
2. Avisar explícitamente en el navegador cuando el periodo es solo un
   año (confirm() en static/js/confirmaciones.js, ver
   test_xss_confirmaciones_inline.py para el patrón de esta app) -- eso
   no se puede probar sin un navegador real, así que aquí solo se
   verifica el lado servidor (formato) y que la plantilla quede
   preparada para el aviso (id del campo + clase del formulario).
"""

from decimal import Decimal

from tests.conftest import crear_plan, crear_alumno, crear_usuario, login

from modelos import Beca


def _otorgar_beca(client, matricula, periodo_escolar, **overrides):
    datos = {
        'nombre': 'Beca de Prueba',
        'tipo_descuento': 'PORCENTAJE',
        'valor': '20',
        'periodo_escolar': periodo_escolar,
    }
    datos.update(overrides)
    return client.post(f'/alumno/{matricula}/becas', data=datos, follow_redirects=True)


def _preparar(app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    crear_usuario()
    return alumno


def test_rechaza_periodo_escolar_con_texto_arbitrario(client, app):
    alumno = _preparar(app)
    login(client, 'directivo1', 'clave12345')

    respuesta = _otorgar_beca(client, alumno.matricula_id, 'el que sea')

    assert respuesta.status_code == 200  # becas_alumno() responde con redirect + flash en error, no 400
    assert Beca.query.filter_by(matricula_fk=alumno.matricula_id).count() == 0


def test_rechaza_periodo_escolar_con_mas_de_una_letra(client, app):
    alumno = _preparar(app)
    login(client, 'directivo1', 'clave12345')

    respuesta = _otorgar_beca(client, alumno.matricula_id, '2026-BB')

    assert respuesta.status_code == 200
    assert Beca.query.filter_by(matricula_fk=alumno.matricula_id).count() == 0


def test_acepta_periodo_escolar_con_cuatrimestre(client, app):
    alumno = _preparar(app)
    login(client, 'directivo1', 'clave12345')

    respuesta = _otorgar_beca(client, alumno.matricula_id, '2026-B')

    assert respuesta.status_code == 200
    assert Beca.query.filter_by(matricula_fk=alumno.matricula_id).count() == 1


def test_acepta_periodo_escolar_de_solo_anio(client, app):
    """Comportamiento intencional: una beca anual sin cuatrimestre específico."""
    alumno = _preparar(app)
    login(client, 'directivo1', 'clave12345')

    respuesta = _otorgar_beca(client, alumno.matricula_id, '2026')

    assert respuesta.status_code == 200
    assert Beca.query.filter_by(matricula_fk=alumno.matricula_id).count() == 1


def test_formulario_de_becas_prepara_el_aviso_de_confirmacion(client, app):
    """
    La plantilla debe quedar lista para que confirmaciones.js intercepte
    el submit y pregunte antes de aplicar una beca a todo un año.
    """
    alumno = _preparar(app)
    login(client, 'directivo1', 'clave12345')

    respuesta = client.get(f'/alumno/{alumno.matricula_id}/becas')

    assert respuesta.status_code == 200
    assert b'js-confirm-beca-anual' in respuesta.data
    assert b'confirmaciones.js' in respuesta.data
