"""
Pruebas del formato de correo en /registro (hallazgo #5.1 de la
auditoría, plan de remediación paso 5.1). El campo es opcional, así que
solo se valida el formato cuando SÍ se captura algo.
"""

from tests.conftest import crear_plan
from tests.test_registro_publico import _datos_con_plan

from app import Alumno


def test_registro_rechaza_correo_con_formato_invalido(client, app):
    plan = crear_plan()

    respuesta = client.post(
        '/registro',
        data=_datos_con_plan(plan.id, correo='esto-no-es-un-correo'),
        follow_redirects=True
    )

    assert respuesta.status_code == 400
    assert Alumno.query.filter_by(curp='LORF050101MDFXYZ09').first() is None


def test_registro_acepta_correo_valido(client, app):
    plan = crear_plan()

    respuesta = client.post(
        '/registro',
        data=_datos_con_plan(plan.id, correo='aspirante@example.com'),
        follow_redirects=True
    )

    assert respuesta.status_code == 200
    assert Alumno.query.filter_by(curp='LORF050101MDFXYZ09').first() is not None


def test_registro_acepta_sin_correo_porque_es_opcional(client, app):
    plan = crear_plan()

    respuesta = client.post(
        '/registro',
        data=_datos_con_plan(plan.id, correo=''),
        follow_redirects=True
    )

    assert respuesta.status_code == 200
    assert Alumno.query.filter_by(curp='LORF050101MDFXYZ09').first() is not None
