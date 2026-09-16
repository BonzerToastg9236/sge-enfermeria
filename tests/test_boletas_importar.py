"""
Pruebas funcionales de la importación masiva de boletas (hallazgo #6 de
la auditoría, plan de remediación paso 6).

NOTA: "calificaciones válidas se guardan" y "una materia/alumno que no
pertenece al plan seleccionado se rechaza" YA estaban cubiertas en
tests/test_escudo_plan_estudios.py
(test_carga_masiva_de_boletas_guarda_calificaciones_validas y
test_carga_masiva_de_boletas_rechaza_alumno_de_otro_plan) -- no se
duplican aquí. Lo que faltaba, y es lo que se agrega en este archivo,
es el caso de un archivo .xlsx corrupto o con extensión falsa.
"""

import io

from tests.conftest import crear_plan, crear_materia, crear_usuario, login
from tests.test_escudo_plan_estudios import _construir_excel_boletas

from app import Calificacion


def _importar_boletas(client, plan, cuatrimestre='1', periodo_escolar='2026-A', archivo=None, nombre_archivo='boletas.xlsx'):
    return client.post(
        '/boletas/importar',
        data={
            'plan_id': str(plan.id),
            'cuatrimestre': cuatrimestre,
            'periodo_escolar': periodo_escolar,
            'archivo_excel': (archivo, nombre_archivo),
        },
        content_type='multipart/form-data',
        follow_redirects=True,
    )


def test_archivo_xlsx_corrupto_no_tumba_la_vista(client, app):
    plan = crear_plan(nombre='Carrera de Prueba', clave='CDP')
    crear_materia(plan, nombre='Materia de Prueba', cuatrimestre=1)
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    archivo = io.BytesIO(b'esto no es un xlsx valido aunque tenga la extension')
    respuesta = _importar_boletas(client, plan, archivo=archivo)

    assert respuesta.status_code == 200
    assert b'No se pudo leer el archivo' in respuesta.data
    assert Calificacion.query.count() == 0


def test_archivo_con_extension_distinta_a_xlsx_se_rechaza(client, app):
    plan = crear_plan(nombre='Carrera de Prueba', clave='CDP')
    crear_materia(plan, nombre='Materia de Prueba', cuatrimestre=1)
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    archivo = io.BytesIO(b'contenido cualquiera')
    respuesta = _importar_boletas(client, plan, archivo=archivo, nombre_archivo='boletas.csv')

    assert respuesta.status_code == 200
    assert b'formato .xlsx' in respuesta.data
    assert Calificacion.query.count() == 0


def test_encabezados_que_no_coinciden_con_las_materias_del_plan_se_rechazan(client, app):
    """
    El archivo se valida contra las materias EXACTAS del plan+cuatrimestre
    seleccionados -- si alguien sube la plantilla de otra combinación, se
    rechaza con un mensaje claro en vez de desalinear columnas con datos.
    """
    plan = crear_plan(nombre='Carrera de Prueba', clave='CDP')
    crear_materia(plan, nombre='Materia Real del Plan', cuatrimestre=1)
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    archivo = _construir_excel_boletas(
        encabezados=['Matrícula', 'Nombre Completo', 'Materia Que No Corresponde'],
        filas=[],
    )
    respuesta = _importar_boletas(client, plan, archivo=archivo)

    assert respuesta.status_code == 200
    assert b'no coinciden con las materias' in respuesta.data
