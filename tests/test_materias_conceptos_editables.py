"""
Materias y conceptos de cobro editables desde el programa (antes: las materias solo se podían agregar o
eliminar-si-nunca-se-usaron; los conceptos no se podían renombrar ni eliminar). Un cambio se refleja en todo
el sistema porque el resto guarda la referencia (id), no una copia del texto; y lo que ya dejó huella se
ARCHIVA/desactiva en vez de borrarse.
"""

from decimal import Decimal

import pytest

from tests.conftest import crear_plan, crear_materia, crear_alumno, crear_usuario, login
from app import db, Alumno, Calificacion, Cargo, ConceptoCobro, EstatusAlumno, EstatusCargo, InscripcionMateria, Pago, RolUsuario
from modelos import BitacoraAuditoria, Materia


def _directivo(client):
    crear_usuario(username='dir1'); login(client, 'dir1', 'clave12345')


def _bitacora(accion):
    db.session.expire_all()
    return BitacoraAuditoria.query.filter_by(accion=accion).all()


def _editar(client, materia, **datos):
    base = dict(nombre=materia.nombre, clave='', cuatrimestre=str(materia.cuatrimestre), creditos='')
    base.update(datos)
    return client.post(f'/materias/{materia.id}/editar', data=base)


# ------------------------------- materias: editar -------------------------------

def test_editar_una_materia_se_guarda_y_queda_en_bitacora(client, app):
    plan = crear_plan(); m = crear_materia(plan, 'Anatomia', 1)
    _directivo(client)
    _editar(client, m, nombre='Anatomía y Fisiología I', clave='ENF-101', cuatrimestre='2', creditos='8.5')
    db.session.expire_all()
    m2 = db.session.get(Materia, m.id)
    assert (m2.nombre, m2.clave, m2.cuatrimestre, m2.creditos, m2.activa) == ('Anatomía y Fisiología I', 'ENF-101', 2, 8.5, True)
    (reg,) = _bitacora('MATERIA_EDITADA')
    assert 'Anatomia' in reg.detalle and 'Anatomía y Fisiología I' in reg.detalle and '1' in reg.detalle and '2' in reg.detalle


@pytest.mark.parametrize('campo,valor', [
    ('nombre', 'ab'), ('nombre', 'N' * 151), ('clave', 'C' * 21),
    ('cuatrimestre', '0'), ('cuatrimestre', '10'), ('cuatrimestre', '²'), ('cuatrimestre', 'x'),
    ('creditos', 'nan'), ('creditos', 'inf'), ('creditos', '-1'), ('creditos', '1e3'), ('creditos', '1000'),
])
def test_editar_materia_valida_cada_campo(client, app, campo, valor):
    plan = crear_plan(); m = crear_materia(plan, 'Anatomia', 1)
    _directivo(client)
    r = _editar(client, m, **{campo: valor})
    assert r.status_code == 302
    db.session.expire_all()
    m2 = db.session.get(Materia, m.id)
    assert (m2.nombre, m2.cuatrimestre) == ('Anatomia', 1)


def test_editar_no_puede_repetir_otra_materia_del_mismo_cuatrimestre(client, app):
    plan = crear_plan(); crear_materia(plan, 'Anatomia', 1); b = crear_materia(plan, 'Fisiologia', 1)
    _directivo(client)
    _editar(client, b, nombre='Anatomia')
    db.session.expire_all()
    assert db.session.get(Materia, b.id).nombre == 'Fisiologia'


def test_renombrar_una_materia_con_calificaciones_se_refleja_en_el_historial_sin_perder_nada(client, app):
    plan = crear_plan(); m = crear_materia(plan, 'Anatomia', 1); a = crear_alumno(plan)
    db.session.add(Calificacion(matricula_fk=a.matricula_id, id_materia_fk=m.id, calificacion_final=9.0, periodo_escolar='2026-A'))
    db.session.commit()
    _directivo(client)
    _editar(client, m, nombre='Anatomía Humana', cuatrimestre='2')
    html = client.get(f'/alumno/{a.matricula_id}/expediente').get_data(as_text=True)
    assert 'Anatomía Humana' in html and 'Anatomia<' not in html
    assert Calificacion.query.one().calificacion_final == 9.0


@pytest.mark.parametrize('rol', [RolUsuario.ADMINISTRATIVO, RolUsuario.CONTADOR, RolUsuario.CAPTURADOR])
def test_solo_direccion_edita_materias(client, app, rol):
    plan = crear_plan(); m = crear_materia(plan, 'Anatomia', 1)
    crear_usuario(username='otro', rol=rol); login(client, 'otro', 'clave12345')
    _editar(client, m, nombre='Hackeada'); client.post(f'/materias/{m.id}/toggle')
    db.session.expire_all()
    m2 = db.session.get(Materia, m.id)
    assert m2.nombre == 'Anatomia' and m2.activa


# ------------------------------- materias: archivar / eliminar -------------------------------

def test_archivar_una_materia_la_saca_de_boletas_carga_academica_y_egreso_pero_conserva_su_historia(client, app):
    plan = crear_plan(); vieja = crear_materia(plan, 'Materia Descontinuada', 1); vigente = crear_materia(plan, 'Materia Vigente', 1)
    a = crear_alumno(plan)                                                # ACTIVO, sin calificaciones
    _directivo(client)
    client.post(f'/materias/{vieja.id}/toggle')
    client.get(f'/planes/{plan.id}/materias')             # consume el aviso de la acción (menciona la materia)
    db.session.expire_all()
    assert db.session.get(Materia, vieja.id).activa is False
    assert len(_bitacora('MATERIA_ARCHIVADA')) == 1

    boleta = client.get(f'/alumno/{a.matricula_id}/boleta?cuatrimestre=1').get_data(as_text=True)
    assert 'Materia Vigente' in boleta and 'Materia Descontinuada' not in boleta

    # egreso: la materia archivada no cuenta como "sin aprobar"
    db.session.add(Calificacion(matricula_fk=a.matricula_id, id_materia_fk=vigente.id, calificacion_final=9.0, periodo_escolar='2026-A')); db.session.commit()
    client.post(f'/alumno/{a.matricula_id}/cambiar-estatus', data={'nuevo_estatus': 'EGRESADO'})
    db.session.expire_all()
    assert db.session.get(Alumno, a.matricula_id).estatus == EstatusAlumno.EGRESADO

    client.post(f'/materias/{vieja.id}/toggle')                           # reactivar
    db.session.expire_all()
    assert db.session.get(Materia, vieja.id).activa is True
    assert len(_bitacora('MATERIA_REACTIVADA')) == 1


def test_una_materia_archivada_no_entra_en_la_carga_academica_de_nuevos_alumnos(client, app):
    plan = crear_plan(); vieja = crear_materia(plan, 'Materia Descontinuada', 1); crear_materia(plan, 'Materia Vigente', 1)
    vieja.activa = False; db.session.commit()
    a = crear_alumno(plan, estatus=EstatusAlumno.PENDIENTE)
    _directivo(client)
    client.post(f'/alumno/{a.matricula_id}/cambiar-estatus', data={'nuevo_estatus': 'ACTIVO'})
    assert [i.materia.nombre for i in InscripcionMateria.query.all()] == ['Materia Vigente']


def test_la_plantilla_de_boletas_solo_trae_materias_activas(client, app):
    import io, openpyxl
    plan = crear_plan(); vieja = crear_materia(plan, 'Materia Descontinuada', 1); crear_materia(plan, 'Materia Vigente', 1)
    vieja.activa = False; db.session.commit(); crear_alumno(plan)
    _directivo(client)
    r = client.get(f'/boletas/importar/plantilla?plan_id={plan.id}&cuatrimestre=1')
    ws = openpyxl.load_workbook(io.BytesIO(r.data))['Calificaciones']
    assert [c.value for c in ws[1]] == ['Matrícula', 'Nombre Completo', 'Materia Vigente']


def test_eliminar_una_materia_sin_uso_sigue_funcionando_y_con_uso_sugiere_archivarla(client, app):
    plan = crear_plan(); libre = crear_materia(plan, 'Libre', 1); usada = crear_materia(plan, 'Usada', 1); a = crear_alumno(plan)
    db.session.add(Calificacion(matricula_fk=a.matricula_id, id_materia_fk=usada.id, calificacion_final=8.0, periodo_escolar='2026-A')); db.session.commit()
    _directivo(client)
    client.post(f'/materias/{libre.id}/eliminar')
    r = client.post(f'/materias/{usada.id}/eliminar', follow_redirects=True)
    assert Materia.query.filter_by(nombre='Libre').count() == 0 and Materia.query.filter_by(nombre='Usada').count() == 1
    assert 'archívala' in r.get_data(as_text=True).lower()


# ------------------------------- conceptos: renombrar / eliminar / aplicar precio -------------------------------

def _concepto(nombre, monto=None, **kw):
    c = ConceptoCobro(nombre=nombre, monto_sugerido=monto, activo=True, **kw)
    db.session.add(c); db.session.commit()
    return c


def _cargo(alumno, concepto, monto='100.00', periodo='P', **kw):
    c = Cargo(matricula_fk=alumno.matricula_id, concepto_cobro_fk=concepto.id, concepto=concepto.nombre, monto=Decimal(monto), periodo_escolar=periodo, **kw)
    db.session.add(c); db.session.commit()
    return c


def test_renombrar_un_concepto_actualiza_los_cargos_existentes(client, app):
    plan = crear_plan(); a = crear_alumno(plan)
    viejo = _concepto('Colegiatura'); otro = _concepto('Uniformes')
    c1 = _cargo(a, viejo, periodo='P1'); c2 = _cargo(a, otro, periodo='P2')
    _directivo(client)
    client.post(f'/conceptos-cobro/{viejo.id}/renombrar', data={'nombre': 'Mensualidad Escolar'})
    db.session.expire_all()
    assert db.session.get(Cargo, c1.id).concepto == 'Mensualidad Escolar' and db.session.get(Cargo, c2.id).concepto == 'Uniformes'
    assert db.session.get(ConceptoCobro, viejo.id).nombre == 'Mensualidad Escolar'
    (reg,) = _bitacora('CONCEPTO_RENOMBRADO')
    assert 'Colegiatura' in reg.detalle and 'Mensualidad Escolar' in reg.detalle and '1 cargo' in reg.detalle


@pytest.mark.parametrize('nuevo', ['ab', 'N' * 101, 'Uniformes', 'uniformes '])
def test_renombrar_concepto_valida_largo_y_duplicados(client, app, nuevo):
    a = _concepto('Colegiatura'); _concepto('Uniformes')
    _directivo(client)
    client.post(f'/conceptos-cobro/{a.id}/renombrar', data={'nombre': nuevo})
    db.session.expire_all()
    assert db.session.get(ConceptoCobro, a.id).nombre == 'Colegiatura'


def test_eliminar_concepto_solo_si_nunca_se_uso(client, app):
    plan = crear_plan(); a = crear_alumno(plan)
    libre = _concepto('Libre'); usado = _concepto('Usado'); _cargo(a, usado)
    _directivo(client)
    client.post(f'/conceptos-cobro/{libre.id}/eliminar')
    r = client.post(f'/conceptos-cobro/{usado.id}/eliminar', follow_redirects=True)
    assert ConceptoCobro.query.filter_by(nombre='Libre').count() == 0 and ConceptoCobro.query.filter_by(nombre='Usado').count() == 1
    assert 'desactiv' in r.get_data(as_text=True).lower()
    assert len(_bitacora('CONCEPTO_ELIMINADO')) == 1


def test_cambiar_un_precio_no_toca_cargos_ya_generados_salvo_que_se_pida_y_solo_pendientes_sin_pagos(client, app):
    plan = crear_plan()
    alumnos = [crear_alumno(plan, curp=f'DDDD0101{i:02d}HDFXYZ01', matricula_id=f'TST2026-{i:05d}') for i in range(1, 5)]
    unif = _concepto('Uniformes', Decimal('300.00'))
    cargos = [_cargo(a, unif, '300.00', f'U{i}') for i, a in enumerate(alumnos)]
    db.session.add(Pago(cargo_fk=cargos[1].id, monto_pagado=Decimal('50.00'), folio='PAGO-9')); cargos[1].estatus = EstatusCargo.PARCIAL
    cargos[2].estatus = EstatusCargo.PAGADO
    db.session.commit()
    _directivo(client)
    client.post(f'/conceptos-cobro/{unif.id}/editar-precio', data={'monto_sugerido': '350.00'})
    db.session.expire_all()
    assert {db.session.get(Cargo, c.id).monto for c in cargos} == {Decimal('300.00')}
    client.post(f'/conceptos-cobro/{unif.id}/editar-precio', data={'monto_sugerido': '400.00', 'aplicar_pendientes': 'on'})
    db.session.expire_all()
    assert [db.session.get(Cargo, c.id).monto for c in cargos] == [Decimal('400.00'), Decimal('300.00'), Decimal('300.00'), Decimal('400.00')]
    assert '2 cargo' in _bitacora('PRECIO_CONCEPTO')[-1].detalle
