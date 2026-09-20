"""
Validación previa al despliegue (2026-09-20), corrida contra PostgreSQL 16 real:
SQLite no aplica el largo de las columnas ni rechaza bytes nulos ni desbordes,
PostgreSQL sí, y ahí varias entradas daban 500 (algunas a cualquier anónimo en
el registro público). También: un doble clic en "generar mensualidades" daba
500 en vez del aviso, y importar 2000 alumnos con cargos tarda ~80 s en
PostgreSQL (Gunicorn y Nginx cortan a los 60 s: el lote quedaba a medias).
"""

import io
from decimal import Decimal
from unittest import mock

import pytest
from openpyxl import Workbook
from sqlalchemy.exc import DataError

from tests.conftest import crear_plan, crear_alumno, crear_usuario, login
from tests.test_importacion_endurecida import COLUMNAS, _fila
from app import db, Alumno, Cargo, ConceptoCobro, RolUsuario
from utilidades.archivos import MAX_FILAS_IMPORTACION


FORMULARIO = dict(
    nombre_completo='Maria Fernanda Lopez', curp='LOPM050101MDFPRR09', fecha_nacimiento='2005-01-01',
    fecha_certificado_prepa='2023-07-01', sexo='Femenino', domicilio_calle_numero='Calle 1',
    domicilio_ciudad='CDMX', domicilio_cp='01000', domicilio_estado='CDMX',
    contacto_emergencia_nombre='Mama', contacto_emergencia_telefono='5500000000',
    turno='MATUTINO', modalidad='ESCOLARIZADO',
)


def _registro(client, plan, **extra):
    return client.post('/registro', data=dict(FORMULARIO, id_plan_fk=plan.id, **extra))


# ---------------------------- registro público ----------------------------

@pytest.mark.parametrize('campo,valor,columna', [
    ('telefono', '9' * 200, 'telefono'),
    ('tipo_sangre', 'O+' * 20, 'tipo_sangre'),
    ('domicilio_cp', '1' * 40, 'domicilio_cp'),
    ('como_se_entero', 'x' * 500, 'como_se_entero'),
    ('domicilio_calle_numero', 'c' * 300, 'domicilio_calle_numero'),
])
def test_el_registro_publico_rechaza_campos_mas_largos_que_su_columna(client, app, campo, valor, columna):
    plan = crear_plan()
    r = _registro(client, plan, **{campo: valor})
    assert r.status_code == 400
    assert columna in r.get_data(as_text=True)
    assert Alumno.query.count() == 0


def test_el_registro_publico_con_datos_normales_sigue_funcionando(client, app):
    plan = crear_plan()
    assert _registro(client, plan).status_code == 302
    assert Alumno.query.count() == 1


# ---------------------------- bytes nulos / paginación ----------------------------

def test_un_byte_nulo_en_un_formulario_o_en_la_url_da_400_y_no_500(client, app):
    plan = crear_plan()
    assert _registro(client, plan, telefono='55\x0012').status_code == 400
    assert client.get('/login?next=%00').status_code == 400
    assert Alumno.query.count() == 0


@pytest.mark.parametrize('url', ['/?filtro=activos', '/reportes/cartera-vencida', '/auditoria'])
def test_una_pagina_absurdamente_grande_no_da_500(client, app, url):
    crear_usuario(); login(client, 'directivo1', 'clave12345')
    separador = '&' if '?' in url else '?'
    assert client.get(f'{url}{separador}page=99999999999999999999').status_code == 200


# ---------------------------- documentos: cuatrimestre acotado ----------------------------

@pytest.mark.parametrize('valor', ['0', '500', '99999999999', '²', '-1', 'abc'])
def test_el_cuatrimestre_del_expediente_respeta_el_maximo_configurado(client, app, valor):
    plan = crear_plan(); alumno = crear_alumno(plan)
    crear_usuario(username='cap', rol=RolUsuario.CAPTURADOR); login(client, 'cap', 'clave12345')
    r = client.post(f'/alumno/{alumno.matricula_id}/documentos', data={'cuatrimestre_actual': valor})
    assert r.status_code == 302
    db.session.expire_all()
    assert db.session.get(Alumno, alumno.matricula_id).cuatrimestre_actual == 1


# ---------------------------- red de seguridad ante DataError ----------------------------

def test_un_error_de_dato_fuera_de_rango_de_la_bd_se_convierte_en_aviso_no_en_500(client, app):
    crear_usuario(); login(client, 'directivo1', 'clave12345')
    with mock.patch('rutas.usuarios.registrar', side_effect=DataError('INSERT', {}, Exception('value too long'))):
        r = client.post('/usuarios/nuevo', data={'nombre_completo': 'Ana Lopez', 'username': 'ana.lopez', 'rol': 'CONTADOR',
                                                 'password': 'Turno-Vespertino#41', 'confirmar_password': 'Turno-Vespertino#41'},
                        follow_redirects=True)
    assert r.status_code == 200
    assert 'longitud' in r.get_data(as_text=True)


# ---------------------------- lote simultáneo ----------------------------

def test_un_lote_de_mensualidades_que_choca_con_otro_simultaneo_avisa_en_vez_de_dar_500(client, app):
    """Simula la carrera: _cargo_duplicado() no ve el cargo que otra petición ya insertó; el índice único lo detiene."""
    plan = crear_plan(); plan.monto_mensualidad = Decimal('2500.00'); db.session.commit()
    alumno = crear_alumno(plan, curp='AAAA010101HDFXYZ01', matricula_id='TST2026-00001')
    crear_alumno(plan, curp='BBBB010101HDFXYZ02', matricula_id='TST2026-00002')   # con 2+ alumnos el choque ocurre en el autoflush de la iteración siguiente
    mens = ConceptoCobro(nombre='Mensualidad', es_mensualidad=True, activo=True)
    db.session.add(mens); db.session.commit()
    db.session.add(Cargo(matricula_fk=alumno.matricula_id, concepto_cobro_fk=mens.id, concepto='Mensualidad',
                         monto=Decimal('2500.00'), periodo_escolar='2026-C-Sep'))
    db.session.commit()
    crear_usuario(); login(client, 'directivo1', 'clave12345')
    with mock.patch('rutas.cobros._cargo_duplicado', return_value=None):
        r = client.post('/cobros/generar-mensualidades', data={'concepto_cobro_id': mens.id, 'periodo_escolar': '2026-C-Sep', 'fecha_vencimiento': '2026-09-10'})
    assert r.status_code == 200
    assert 'No se generó el lote' in r.get_data(as_text=True)
    assert Cargo.query.count() == 1     # ni el del alumno 2: el lote es todo o nada


# ---------------------------- tamaño del lote de importación ----------------------------

def test_la_importacion_rechaza_mas_filas_de_las_que_caben_en_el_timeout(client, app):
    crear_plan(clave='LEN'); crear_usuario(); login(client, 'directivo1', 'clave12345')
    wb = Workbook(); ws = wb.active; ws.append(COLUMNAS)
    for i in range(MAX_FILAS_IMPORTACION + 1):
        ws.append(_fila(i))
    b = io.BytesIO(); wb.save(b); b.seek(0)
    r = client.post('/alumnos/importar', data={'archivo_excel': (b, 'a.xlsx')}, content_type='multipart/form-data', follow_redirects=True)
    assert 'demasiadas filas' in r.get_data(as_text=True).lower()
    assert Alumno.query.count() == 0


def test_el_tope_de_filas_deja_margen_bajo_el_timeout_de_gunicorn():
    # ~42 ms por alumno con cargos medidos en PostgreSQL 16 -> 500 filas ~ 21 s de 60 s.
    assert MAX_FILAS_IMPORTACION * 0.042 <= 60 / 2
