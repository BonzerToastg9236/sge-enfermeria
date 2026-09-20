"""
Cada institución tiene sus propias carreras, materias y precios. Hasta ahora las carreras solo las
creaba seed.py y no se podían editar ni desactivar desde el programa (solo el precio de la mensualidad).
Estas pruebas fijan: crear / editar / desactivar / eliminar carreras, y que el cambio se vea en todo el
sistema (registro público, expedientes) y quede en la bitácora.
"""

from decimal import Decimal

import pytest

from tests.conftest import crear_plan, crear_alumno, crear_usuario, login
from app import db, Cargo, ConceptoCobro, EstatusCargo, PlanEstudio, Pago, RolUsuario, Materia
from modelos import Beca, BitacoraAuditoria, TipoDescuentoBeca


def _directivo(client):
    crear_usuario(username='dir1'); login(client, 'dir1', 'clave12345')


def _nuevo(client, **datos):
    base = dict(nombre='Licenciatura en Nutrición', clave_carrera='LNU', anio_generacion='2026', duracion_anios='4', monto_mensualidad='')
    base.update(datos)
    return client.post('/planes/nuevo', data=base)


def _bitacora(accion):
    db.session.expire_all()
    return BitacoraAuditoria.query.filter_by(accion=accion).all()


# ------------------------------- crear -------------------------------

def test_crear_una_carrera_desde_el_programa(client, app):
    _directivo(client)
    _nuevo(client, monto_mensualidad='2500.50')
    plan = PlanEstudio.query.filter_by(clave_carrera='LNU').one()
    assert (plan.nombre, plan.anio_generacion, plan.duracion_anios, plan.monto_mensualidad, plan.activo) == \
        ('Licenciatura en Nutrición', 2026, 4, Decimal('2500.50'), True)
    (reg,) = _bitacora('PLAN_CREADO')
    assert 'LNU' in reg.detalle and 'Nutrición' in reg.detalle


def test_la_clave_se_guarda_en_mayusculas(client, app):
    _directivo(client)
    _nuevo(client, clave_carrera='lnu')
    assert PlanEstudio.query.filter_by(clave_carrera='LNU').count() == 1


@pytest.mark.parametrize('campo,valor', [
    ('nombre', 'ab'), ('nombre', 'N' * 151),
    ('clave_carrera', ''), ('clave_carrera', 'L N'), ('clave_carrera', 'LNU-1'), ('clave_carrera', 'ABCDEFGHIJK'),
    ('anio_generacion', '1999'), ('anio_generacion', '2101'), ('anio_generacion', 'abc'),
    ('duracion_anios', '0'), ('duracion_anios', '50'), ('duracion_anios', 'x'),
    ('monto_mensualidad', 'Infinity'), ('monto_mensualidad', '-5'), ('monto_mensualidad', '0.001'),
])
def test_crear_carrera_valida_cada_campo(client, app, campo, valor):
    _directivo(client)
    r = _nuevo(client, **{campo: valor})
    assert r.status_code == 302
    assert PlanEstudio.query.count() == 0


def test_no_se_puede_repetir_clave_y_anio(client, app):
    crear_plan(clave='LNU', anio=2026)
    _directivo(client)
    _nuevo(client)
    assert PlanEstudio.query.filter_by(clave_carrera='LNU').count() == 1
    _nuevo(client, anio_generacion='2027')          # otra generación de la misma carrera SÍ
    assert PlanEstudio.query.filter_by(clave_carrera='LNU').count() == 2


@pytest.mark.parametrize('rol', [RolUsuario.ADMINISTRATIVO, RolUsuario.CONTADOR, RolUsuario.CAPTURADOR])
def test_solo_direccion_administra_carreras(client, app, rol):
    plan = crear_plan()
    crear_usuario(username='otro', rol=rol); login(client, 'otro', 'clave12345')
    _nuevo(client)
    client.post(f'/planes/{plan.id}/editar', data={'nombre': 'Hackeada', 'clave_carrera': 'TST', 'anio_generacion': '2026', 'duracion_anios': ''})
    client.post(f'/planes/{plan.id}/toggle'); client.post(f'/planes/{plan.id}/eliminar')
    db.session.expire_all()
    p = db.session.get(PlanEstudio, plan.id)
    assert PlanEstudio.query.count() == 1 and p.nombre != 'Hackeada' and p.activo


# ------------------------------- editar -------------------------------

def test_editar_una_carrera_se_guarda_y_queda_en_bitacora(client, app):
    plan = crear_plan(nombre='Nombre viejo', clave='OLD', anio=2026)
    _directivo(client)
    client.post(f'/planes/{plan.id}/editar', data={'nombre': 'Licenciatura en Enfermería y Obstetricia', 'clave_carrera': 'leo',
                                                    'anio_generacion': '2027', 'duracion_anios': '4'})
    db.session.expire_all()
    p = db.session.get(PlanEstudio, plan.id)
    assert (p.nombre, p.clave_carrera, p.anio_generacion, p.duracion_anios) == ('Licenciatura en Enfermería y Obstetricia', 'LEO', 2027, 4)
    (reg,) = _bitacora('PLAN_EDITADO')
    assert 'Nombre viejo' in reg.detalle and 'Obstetricia' in reg.detalle and 'OLD' in reg.detalle and 'LEO' in reg.detalle


def test_editar_no_puede_chocar_con_otra_carrera(client, app):
    crear_plan(clave='AAA', anio=2026)
    otro = crear_plan(nombre='Otra', clave='BBB', anio=2026)
    _directivo(client)
    client.post(f'/planes/{otro.id}/editar', data={'nombre': 'Otra', 'clave_carrera': 'AAA', 'anio_generacion': '2026', 'duracion_anios': ''})
    db.session.expire_all()
    assert db.session.get(PlanEstudio, otro.id).clave_carrera == 'BBB'


def test_el_nuevo_nombre_se_refleja_en_todo_el_sistema_y_las_matriculas_existentes_no_cambian(client, app):
    plan = crear_plan(nombre='Nombre viejo', clave='OLD')
    alumno = crear_alumno(plan)
    matricula = alumno.matricula_id
    _directivo(client)
    client.post(f'/planes/{plan.id}/editar', data={'nombre': 'Nombre nuevo oficial', 'clave_carrera': 'NEW', 'anio_generacion': '2026', 'duracion_anios': ''})
    assert 'Nombre nuevo oficial' in client.get('/registro').get_data(as_text=True)
    assert 'Nombre nuevo oficial' in client.get(f'/alumno/{matricula}/ficha').get_data(as_text=True)
    assert 'Nombre viejo' not in client.get('/registro').get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(type(alumno), matricula) is not None      # la matrícula ya emitida es intocable


# ------------------------------- desactivar / eliminar -------------------------------

def test_una_carrera_desactivada_no_se_ofrece_a_los_aspirantes_y_se_puede_reactivar(client, app):
    plan = crear_plan(nombre='Carrera que cierra')
    _directivo(client)
    client.post(f'/planes/{plan.id}/toggle')
    client.get('/planes/mensualidades')                    # consume el aviso de la acción (menciona el nombre)
    assert 'Carrera que cierra' not in client.get('/registro').get_data(as_text=True)
    (reg,) = _bitacora('PLAN_DESACTIVADO')
    client.post(f'/planes/{plan.id}/toggle')
    client.get('/planes/mensualidades')
    assert 'Carrera que cierra' in client.get('/registro').get_data(as_text=True)


def test_eliminar_una_carrera_sin_alumnos_borra_tambien_sus_materias(client, app):
    plan = crear_plan()
    db.session.add(Materia(nombre='Anatomia', cuatrimestre=1, id_plan_fk=plan.id)); db.session.commit()
    _directivo(client)
    client.post(f'/planes/{plan.id}/eliminar')
    assert PlanEstudio.query.count() == 0 and Materia.query.count() == 0
    assert len(_bitacora('PLAN_ELIMINADO')) == 1


def test_no_se_elimina_una_carrera_que_ya_tiene_alumnos(client, app):
    plan = crear_plan(); crear_alumno(plan)
    _directivo(client)
    client.post(f'/planes/{plan.id}/eliminar')
    assert PlanEstudio.query.count() == 1
    assert _bitacora('PLAN_ELIMINADO') == []


# ------------------------------- mensualidad: aplicar a lo pendiente -------------------------------

def _mensualidades(plan, n=3):
    mens = ConceptoCobro.query.filter_by(es_mensualidad=True).first() or ConceptoCobro(nombre='Mensualidad', es_mensualidad=True, activo=True)
    db.session.add(mens); db.session.commit()
    alumnos = [crear_alumno(plan, curp=f'CCCC0101{i:02d}HDFXYZ01', matricula_id=f'TST2026-{i:05d}') for i in range(1, n + 1)]
    cargos = []
    for a in alumnos:
        c = Cargo(matricula_fk=a.matricula_id, concepto_cobro_fk=mens.id, concepto='Mensualidad', monto=Decimal('2000.00'), periodo_escolar='2026-C-Sep')
        db.session.add(c); cargos.append(c)
    db.session.commit()
    return alumnos, cargos


def test_cambiar_la_mensualidad_no_toca_cargos_ya_generados_salvo_que_se_pida(client, app):
    plan = crear_plan(); plan.monto_mensualidad = Decimal('2000.00'); db.session.commit()
    _, cargos = _mensualidades(plan)
    _directivo(client)
    client.post('/planes/mensualidades', data={'plan_id': plan.id, 'monto_mensualidad': '2600.00'})
    db.session.expire_all()
    assert {c.monto for c in Cargo.query.all()} == {Decimal('2000.00')}


def test_aplicar_la_nueva_mensualidad_solo_a_cargos_pendientes_sin_pagos_respetando_becas(client, app):
    plan = crear_plan(); plan.monto_mensualidad = Decimal('2000.00'); db.session.commit()
    alumnos, cargos = _mensualidades(plan, n=4)
    db.session.add(Beca(matricula_fk=alumnos[0].matricula_id, nombre='B', tipo_descuento=TipoDescuentoBeca.PORCENTAJE,
                        valor=Decimal('50'), periodo_escolar='2026-C', activa=True))
    db.session.add(Pago(cargo_fk=cargos[1].id, monto_pagado=Decimal('500.00'), folio='PAGO-1'))     # ya tiene un pago
    cargos[1].estatus = EstatusCargo.PARCIAL
    cargos[2].estatus = EstatusCargo.CANCELADO
    db.session.commit()
    _directivo(client)
    client.post('/planes/mensualidades', data={'plan_id': plan.id, 'monto_mensualidad': '2600.00', 'aplicar_pendientes': 'on'})
    db.session.expire_all()
    montos = [db.session.get(Cargo, c.id).monto for c in cargos]
    assert montos == [Decimal('1300.00'), Decimal('2000.00'), Decimal('2000.00'), Decimal('2600.00')]
    (reg,) = _bitacora('PRECIO_MENSUALIDAD')
    assert '2 cargo' in reg.detalle or '2 cargos' in reg.detalle
