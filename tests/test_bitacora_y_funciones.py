"""
Auditoría 2026-09-19 (integridad y separación de funciones):
  * cancelar un cargo no pedía motivo ni dejaba rastro de QUIÉN lo hizo;
  * condonar el recargo de un cargo ya pagado dejaba saldo NEGATIVO (dinero
    sin cargo) que además enmascara deudas reales;
  * cambios de precios, recargos, becas y cuentas de usuario no dejaban bitácora;
  * la misma referencia bancaria se aceptaba en pagos de alumnos distintos;
  * un CAPTURADOR (sin permiso de cobros) veía el monto exacto del adeudo y
    la lista de quién debe, y los cargos que dispara al activar/avanzar
    alumnos no quedaban atribuidos a nadie.
"""

from decimal import Decimal

import pytest

from tests.conftest import crear_plan, crear_alumno, crear_usuario, login
from tests.test_importacion_endurecida import _xlsx, _fila
from app import (
    db, Cargo, ConceptoCobro, ConfiguracionCobros, EstatusAlumno, EstatusCargo, Pago, RolUsuario, Usuario,
)
from modelos import Beca, BitacoraAuditoria


def _sesion(client, rol, username=None):
    username = username or rol.name.lower()
    usuario = crear_usuario(username=username, rol=rol)
    login(client, username, 'clave12345')
    return usuario


def _concepto(nombre='Uniformes', monto=None, **kw):
    c = ConceptoCobro(nombre=nombre, monto_sugerido=monto, activo=True, **kw)
    db.session.add(c); db.session.commit()
    return c


def _cargo(alumno, concepto, monto='1000.00', periodo='P', **kw):
    c = Cargo(matricula_fk=alumno.matricula_id, concepto_cobro_fk=concepto.id, concepto=concepto.nombre,
              monto=Decimal(monto), periodo_escolar=periodo, **kw)
    db.session.add(c); db.session.commit()
    return c


def _registros(accion):
    db.session.expire_all()
    return BitacoraAuditoria.query.filter_by(accion=accion).all()


# ---------------------------------------------------------------------------
# Cancelar cargo: motivo obligatorio + bitácora
# ---------------------------------------------------------------------------

def test_cancelar_un_cargo_exige_motivo(client, app):
    plan = crear_plan(); alumno = crear_alumno(plan); cargo = _cargo(alumno, _concepto())
    _sesion(client, RolUsuario.CONTADOR)
    client.post(f'/cobro/{cargo.id}/cancelar', data={'comentario': ''})
    client.post(f'/cobro/{cargo.id}/cancelar', data={'comentario': 'abc'})
    db.session.expire_all()
    assert db.session.get(Cargo, cargo.id).estatus == EstatusCargo.PENDIENTE
    assert _registros('CARGO_CANCELADO') == []


def test_cancelar_un_cargo_deja_bitacora_con_quien_cuando_y_por_que(client, app):
    plan = crear_plan(); alumno = crear_alumno(plan); cargo = _cargo(alumno, _concepto())
    usuario = _sesion(client, RolUsuario.CONTADOR)
    client.post(f'/cobro/{cargo.id}/cancelar', data={'comentario': 'Cargo duplicado por error de captura'})
    (reg,) = _registros('CARGO_CANCELADO')
    assert (reg.usuario_fk, reg.usuario_nombre) == (usuario.id, usuario.nombre_completo)
    assert (reg.entidad, reg.entidad_id, reg.matricula_fk) == ('Cargo', str(cargo.id), alumno.matricula_id)
    assert 'Cargo duplicado por error de captura' in reg.detalle and '1000.00' in reg.detalle
    assert reg.fecha is not None


# ---------------------------------------------------------------------------
# Condonar recargo: nunca deja saldo negativo
# ---------------------------------------------------------------------------

def _cargo_con_recargo_y_pago(client, pagado):
    plan = crear_plan(); alumno = crear_alumno(plan)
    cargo = _cargo(alumno, _concepto(), monto='1000.00', recargo_aplicado=Decimal('200.00'))
    _sesion(client, RolUsuario.DIRECTIVO)
    client.post(f'/cobro/{cargo.id}/pagar', data={'monto_pagado': pagado, 'metodo_pago': 'EFECTIVO'})
    return alumno, cargo


def test_no_se_puede_condonar_el_recargo_de_un_cargo_ya_pagado(client, app):
    _, cargo = _cargo_con_recargo_y_pago(client, '1200.00')
    client.post(f'/cobro/{cargo.id}/condonar-recargo', data={'nuevo_recargo': '0', 'motivo_condonacion': 'amistad'})
    db.session.expire_all()
    c = db.session.get(Cargo, cargo.id)
    assert c.recargo_aplicado == Decimal('200.00') and c.saldo_pendiente() == Decimal('0.00')
    assert _registros('RECARGO_CONDONADO') == []


def test_condonar_hasta_dejar_el_saldo_en_cero_si_es_valido_y_queda_en_bitacora(client, app):
    alumno, cargo = _cargo_con_recargo_y_pago(client, '1100.00')
    client.post(f'/cobro/{cargo.id}/condonar-recargo', data={'nuevo_recargo': '50', 'motivo_condonacion': 'error'})   # saldo -50: no
    client.post(f'/cobro/{cargo.id}/condonar-recargo', data={'nuevo_recargo': '100', 'motivo_condonacion': 'beca de excelencia'})
    db.session.expire_all()
    c = db.session.get(Cargo, cargo.id)
    assert c.recargo_aplicado == Decimal('100.00') and c.saldo_pendiente() == Decimal('0.00')
    (reg,) = _registros('RECARGO_CONDONADO')
    assert '200.00' in reg.detalle and '100.00' in reg.detalle and 'beca de excelencia' in reg.detalle
    assert reg.matricula_fk == alumno.matricula_id


# ---------------------------------------------------------------------------
# Otras acciones sensibles dejan bitácora
# ---------------------------------------------------------------------------

def test_otorgar_y_desactivar_una_beca_quedan_en_bitacora(client, app):
    plan = crear_plan(); alumno = crear_alumno(plan)
    _sesion(client, RolUsuario.CONTADOR)
    client.post(f'/alumno/{alumno.matricula_id}/becas', data={
        'nombre': 'Excelencia', 'tipo_descuento': 'PORCENTAJE', 'valor': '50', 'periodo_escolar': '2026-B'})
    beca = Beca.query.one()
    client.post(f'/becas/{beca.id}/desactivar')
    (otorgada,) = _registros('BECA_OTORGADA'); (desactivada,) = _registros('BECA_DESACTIVADA')
    assert 'Excelencia' in otorgada.detalle and '50' in otorgada.detalle and '2026-B' in otorgada.detalle
    assert desactivada.entidad_id == str(beca.id) and desactivada.matricula_fk == alumno.matricula_id


def test_anular_un_pago_queda_en_bitacora(client, app):
    plan = crear_plan(); alumno = crear_alumno(plan); cargo = _cargo(alumno, _concepto())
    _sesion(client, RolUsuario.CONTADOR)
    client.post(f'/cobro/{cargo.id}/pagar', data={'monto_pagado': '300', 'metodo_pago': 'EFECTIVO'})
    pago = Pago.query.one()
    client.post(f'/pago/{pago.id}/anular', data={'motivo_anulacion': 'monto mal capturado'})
    (reg,) = _registros('PAGO_ANULADO')
    assert 'monto mal capturado' in reg.detalle and '300.00' in reg.detalle and reg.matricula_fk == alumno.matricula_id


def test_los_cambios_de_precios_y_recargos_registran_valor_anterior_y_nuevo(client, app):
    plan = crear_plan(); plan.monto_mensualidad = Decimal('2000.00'); db.session.commit()
    concepto = _concepto('Constancias', Decimal('10.00'))
    _sesion(client, RolUsuario.DIRECTIVO)
    client.post(f'/conceptos-cobro/{concepto.id}/editar-precio', data={'monto_sugerido': '25.00'})
    client.post('/planes/mensualidades', data={'plan_id': plan.id, 'monto_mensualidad': '2600.00'})
    client.post('/configuracion/cobros', data={'tipo_recargo': 'POR_DIA', 'valor_recargo': '30', 'dias_gracia': '3'})
    (pc,) = _registros('PRECIO_CONCEPTO'); (pm,) = _registros('PRECIO_MENSUALIDAD'); (cr,) = _registros('CONFIG_RECARGOS')
    assert '10.00' in pc.detalle and '25.00' in pc.detalle and 'Constancias' in pc.detalle
    assert '2000.00' in pm.detalle and '2600.00' in pm.detalle
    assert 'POR_DIA' in cr.detalle and '30.00' in cr.detalle and '3' in cr.detalle


def test_alta_y_baja_de_cuentas_quedan_en_bitacora(client, app):
    _sesion(client, RolUsuario.DIRECTIVO)
    client.post('/usuarios/nuevo', data={'nombre_completo': 'Ana Lopez', 'username': 'ana.lopez', 'rol': 'CONTADOR',
                                         'password': 'Turno-Vespertino#41', 'confirmar_password': 'Turno-Vespertino#41'})
    ana = Usuario.query.filter_by(username='ana.lopez').one()
    client.post(f'/usuarios/{ana.id}/toggle')
    (alta,) = _registros('USUARIO_CREADO'); (baja,) = _registros('USUARIO_DESACTIVADO')
    assert 'ana.lopez' in alta.detalle and 'CONTADOR' in alta.detalle
    assert baja.entidad_id == str(ana.id)


def test_el_lote_de_mensualidades_queda_en_bitacora(client, app):
    plan = crear_plan(); plan.monto_mensualidad = Decimal('2500.00'); db.session.commit()
    crear_alumno(plan, curp='AAAA010101HDFXYZ01', matricula_id='TST2026-00001')
    mens = _concepto('Mensualidad', es_mensualidad=True)
    _sesion(client, RolUsuario.CONTADOR)
    client.post('/cobros/generar-mensualidades', data={'concepto_cobro_id': mens.id, 'periodo_escolar': '2026-C-Sep', 'fecha_vencimiento': '2026-09-10'})
    (reg,) = _registros('LOTE_CARGOS')
    assert '2026-C-Sep' in reg.detalle and '1' in reg.detalle


def test_la_importacion_masiva_queda_en_bitacora(client, app):
    crear_plan(clave='LEN'); _sesion(client, RolUsuario.DIRECTIVO)
    client.post('/alumnos/importar', data={'archivo_excel': (_xlsx([_fila(1), _fila(2)]), 'a.xlsx')}, content_type='multipart/form-data')
    (reg,) = _registros('IMPORTACION_ALUMNOS')
    assert '2 alumno' in reg.detalle


def test_los_cargos_automaticos_de_una_activacion_se_atribuyen_a_quien_activa(client, app):
    plan = crear_plan(); plan.monto_mensualidad = Decimal('2500.00'); db.session.commit()
    _concepto('Inscripción', Decimal('3000.00')); _concepto('Mensualidad', es_mensualidad=True)
    alumno = crear_alumno(plan, estatus=EstatusAlumno.PENDIENTE)
    capturador = _sesion(client, RolUsuario.CAPTURADOR)
    client.post(f'/alumno/{alumno.matricula_id}/cambiar-estatus', data={'nuevo_estatus': 'ACTIVO'})
    (reg,) = _registros('CARGOS_AUTOMATICOS')
    assert reg.usuario_fk == capturador.id and reg.matricula_fk == alumno.matricula_id and '5' in reg.detalle


# ---------------------------------------------------------------------------
# Referencia bancaria
# ---------------------------------------------------------------------------

def test_la_misma_referencia_bancaria_no_se_acepta_para_alumnos_distintos(client, app):
    plan = crear_plan()
    a1 = crear_alumno(plan, curp='AAAA010101HDFXYZ01', matricula_id='TST2026-00001')
    a2 = crear_alumno(plan, curp='BBBB010101HDFXYZ02', matricula_id='TST2026-00002')
    u = _concepto()
    c1 = _cargo(a1, u, '500.00', 'X1'); c2 = _cargo(a2, u, '500.00', 'X2')
    _sesion(client, RolUsuario.CONTADOR)
    client.post(f'/cobro/{c1.id}/pagar', data={'monto_pagado': '500', 'metodo_pago': 'TRANSFERENCIA', 'referencia': 'SPEI-123456'})
    client.post(f'/cobro/{c2.id}/pagar', data={'monto_pagado': '500', 'metodo_pago': 'TRANSFERENCIA', 'referencia': ' spei-123456 '})
    assert Pago.query.count() == 1


def test_una_misma_transferencia_puede_pagar_varios_cargos_del_mismo_alumno(client, app):
    plan = crear_plan(); a1 = crear_alumno(plan); u = _concepto()
    c1 = _cargo(a1, u, '500.00', 'X1'); c2 = _cargo(a1, u, '500.00', 'X2')
    _sesion(client, RolUsuario.CONTADOR)
    for c in (c1, c2):
        client.post(f'/cobro/{c.id}/pagar', data={'monto_pagado': '500', 'metodo_pago': 'TRANSFERENCIA', 'referencia': 'SPEI-999'})
    assert Pago.query.count() == 2


def test_una_referencia_de_un_pago_anulado_se_puede_reutilizar(client, app):
    plan = crear_plan()
    a1 = crear_alumno(plan, curp='AAAA010101HDFXYZ01', matricula_id='TST2026-00001')
    a2 = crear_alumno(plan, curp='BBBB010101HDFXYZ02', matricula_id='TST2026-00002')
    u = _concepto(); c1 = _cargo(a1, u, '500.00', 'X1'); c2 = _cargo(a2, u, '500.00', 'X2')
    _sesion(client, RolUsuario.CONTADOR)
    client.post(f'/cobro/{c1.id}/pagar', data={'monto_pagado': '500', 'metodo_pago': 'TRANSFERENCIA', 'referencia': 'SPEI-1'})
    client.post(f'/pago/{Pago.query.one().id}/anular', data={'motivo_anulacion': 'se capturó al alumno equivocado'})
    client.post(f'/cobro/{c2.id}/pagar', data={'monto_pagado': '500', 'metodo_pago': 'TRANSFERENCIA', 'referencia': 'SPEI-1'})
    assert Pago.query.filter_by(anulado=False).count() == 1


# ---------------------------------------------------------------------------
# Separación de funciones: quien no tiene cobros no ve adeudos
# ---------------------------------------------------------------------------

def _alumno_con_adeudo():
    plan = crear_plan(); alumno = crear_alumno(plan, nombre='Alumno Deudor Prueba')
    _cargo(alumno, _concepto(), '1234.56')
    return alumno


def test_el_mensaje_de_adeudo_al_egresar_no_muestra_el_monto_a_un_capturador(client, app):
    alumno = _alumno_con_adeudo()
    _sesion(client, RolUsuario.CAPTURADOR)
    html = client.post(f'/alumno/{alumno.matricula_id}/cambiar-estatus', data={'nuevo_estatus': 'EGRESADO'}, follow_redirects=True).get_data(as_text=True)
    assert 'adeudo' in html and '1234.56' not in html


def test_el_administrativo_si_ve_el_monto_del_adeudo(client, app):
    alumno = _alumno_con_adeudo()
    _sesion(client, RolUsuario.ADMINISTRATIVO)
    html = client.post(f'/alumno/{alumno.matricula_id}/cambiar-estatus', data={'nuevo_estatus': 'EGRESADO'}, follow_redirects=True).get_data(as_text=True)
    assert '1234.56' in html


def test_un_capturador_no_ve_la_tarjeta_ni_la_lista_de_alumnos_con_adeudo(client, app):
    _alumno_con_adeudo()
    _sesion(client, RolUsuario.CAPTURADOR)
    html = client.get('/?filtro=con_adeudo').get_data(as_text=True)
    assert 'Alumnos con Adeudo Económico' not in html and 'Alumno Deudor Prueba' not in html
    assert 'Con Adeudo' not in client.get('/').get_data(as_text=True)


def test_el_contador_si_ve_la_lista_de_alumnos_con_adeudo(client, app):
    _alumno_con_adeudo()
    _sesion(client, RolUsuario.CONTADOR)
    html = client.get('/?filtro=con_adeudo').get_data(as_text=True)
    assert 'Alumno Deudor Prueba' in html


# ---------------------------------------------------------------------------
# Visor de la bitácora
# ---------------------------------------------------------------------------

def _sembrar_bitacora():
    db.session.add_all([
        BitacoraAuditoria(accion='CARGO_CANCELADO', entidad='Cargo', entidad_id='1', matricula_fk='TST2026-00001', detalle='<script>alert(1)</script> motivo', usuario_nombre='Ana'),
        BitacoraAuditoria(accion='BECA_OTORGADA', entidad='Beca', entidad_id='2', matricula_fk='TST2026-00002', detalle='Beca X', usuario_nombre='Luis'),
    ])
    db.session.commit()


def test_solo_direccion_ve_la_bitacora(client, app):
    _sembrar_bitacora()
    _sesion(client, RolUsuario.DIRECTIVO)
    assert client.get('/auditoria').status_code == 200


@pytest.mark.parametrize('rol', [RolUsuario.ADMINISTRATIVO, RolUsuario.CONTADOR, RolUsuario.CAPTURADOR])
def test_los_demas_roles_no_ven_la_bitacora(client, app, rol):
    _sembrar_bitacora()
    _sesion(client, rol)
    assert client.get('/auditoria').status_code == 302


def test_la_bitacora_escapa_el_detalle_y_filtra(client, app):
    _sembrar_bitacora()
    _sesion(client, RolUsuario.DIRECTIVO)
    html = client.get('/auditoria').get_data(as_text=True)
    assert '<script>alert(1)</script>' not in html and 'CARGO_CANCELADO' in html and 'BECA_OTORGADA' in html
    solo = client.get('/auditoria?accion=BECA_OTORGADA').get_data(as_text=True)
    assert 'Beca X' in solo and '&lt;script&gt;' not in solo          # el desplegable siempre lista las acciones; se revisa la tabla
    por_alumno = client.get('/auditoria?q=TST2026-00001').get_data(as_text=True)
    assert '&lt;script&gt;' in por_alumno and 'Beca X' not in por_alumno


def test_la_bitacora_no_tiene_rutas_para_modificarla(app):
    reglas = [r for r in app.url_map.iter_rules() if r.rule.startswith('/auditoria')]
    assert reglas and all(r.methods <= {'GET', 'HEAD', 'OPTIONS'} for r in reglas)


# ---------------------------------------------------------------------------
# Migración al día con los modelos
# ---------------------------------------------------------------------------

def test_las_migraciones_producen_exactamente_el_esquema_de_los_modelos(app):
    import os
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from flask_migrate import upgrade

    db.session.remove(); db.drop_all()
    with db.engine.begin() as cx:
        cx.exec_driver_sql('DROP TABLE IF EXISTS alembic_version')
    try:
        upgrade(directory=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'migrations'))
        with db.engine.connect() as cx:
            diferencias = compare_metadata(MigrationContext.configure(cx, opts={'compare_type': True}), db.metadata)
        assert diferencias == [], f'Falta una migración para los modelos: {diferencias}'
    finally:
        db.session.remove(); db.drop_all()
        with db.engine.begin() as cx:
            cx.exec_driver_sql('DROP TABLE IF EXISTS alembic_version')
        db.create_all()
