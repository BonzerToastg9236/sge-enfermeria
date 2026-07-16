"""
Pruebas del catálogo de conceptos de cobro (solo Directivo lo administra)
y del cálculo de recargos por atraso (auto-ajustable: monto fijo,
porcentaje, o por día).
"""

from datetime import timedelta, datetime
from decimal import Decimal

from tests.conftest import crear_plan, crear_alumno, crear_usuario, login
from app import db, Cargo, EstatusCargo, ConceptoCobro, ConfiguracionCobros, TipoRecargo, RolUsuario


def _crear_cargo_vencido(alumno, dias_vencido=5, monto='1000.00'):
    """Crea un cargo cuya fecha de vencimiento ya pasó hace `dias_vencido` días."""
    hoy = datetime.utcnow().date()
    cargo = Cargo(
        matricula_fk=alumno.matricula_id,
        concepto='Colegiatura de Prueba',
        monto=Decimal(monto),
        fecha_vencimiento=hoy - timedelta(days=dias_vencido),
        estatus=EstatusCargo.PENDIENTE,
    )
    db.session.add(cargo)
    db.session.commit()
    return cargo


def _configurar_recargo(tipo, valor, dias_gracia=0):
    config = ConfiguracionCobros.obtener()
    config.tipo_recargo = tipo
    config.valor_recargo = Decimal(valor)
    config.dias_gracia = dias_gracia
    db.session.commit()
    return config


def test_configuracion_por_defecto_es_neutral(app):
    """Recién sembrado, no debe cobrar nada hasta que Dirección lo configure."""
    config = ConfiguracionCobros.obtener()
    assert config.valor_recargo == Decimal('0.00')


def test_recargo_monto_fijo_se_aplica_una_sola_vez(app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo_vencido(alumno, dias_vencido=10)
    _configurar_recargo(TipoRecargo.MONTO_FIJO, '150.00')

    cargo.actualizar_recargo_si_vencido()

    assert cargo.recargo_aplicado == Decimal('150.00')


def test_recargo_porcentaje_se_calcula_sobre_el_monto_original(app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo_vencido(alumno, dias_vencido=3, monto='1000.00')
    _configurar_recargo(TipoRecargo.PORCENTAJE, '10')  # 10%

    cargo.actualizar_recargo_si_vencido()

    assert cargo.recargo_aplicado == Decimal('100.00')


def test_recargo_por_dia_crece_segun_dias_de_atraso(app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo_vencido(alumno, dias_vencido=7)
    _configurar_recargo(TipoRecargo.POR_DIA, '20.00')  # $20 por día

    cargo.actualizar_recargo_si_vencido()

    assert cargo.recargo_aplicado == Decimal('140.00')  # 7 días x $20


def test_recargo_no_aplica_dentro_del_periodo_de_gracia(app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo_vencido(alumno, dias_vencido=3)
    _configurar_recargo(TipoRecargo.MONTO_FIJO, '150.00', dias_gracia=5)

    cargo.actualizar_recargo_si_vencido()

    assert cargo.recargo_aplicado == Decimal('0.00')


def test_recargo_nunca_disminuye_aunque_la_config_baje(app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo_vencido(alumno, dias_vencido=5)
    _configurar_recargo(TipoRecargo.MONTO_FIJO, '300.00')
    cargo.actualizar_recargo_si_vencido()
    assert cargo.recargo_aplicado == Decimal('300.00')

    # Dirección baja el recargo — el ya aplicado no debe reducirse
    _configurar_recargo(TipoRecargo.MONTO_FIJO, '50.00')
    cargo.actualizar_recargo_si_vencido()

    assert cargo.recargo_aplicado == Decimal('300.00')


def test_administrativo_no_puede_administrar_catalogo_de_conceptos(client, app):
    crear_usuario(username='admin1', password='clave12345', rol=RolUsuario.ADMINISTRATIVO)
    login(client, 'admin1', 'clave12345')

    respuesta = client.get('/conceptos-cobro', follow_redirects=True)

    assert respuesta.status_code == 200
    assert 'permisos'.encode('utf-8') in respuesta.data.lower()


def test_directivo_puede_agregar_concepto_al_catalogo(client, app):
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    client.post('/conceptos-cobro', data={'nombre': 'Cobro de Uniformes'}, follow_redirects=True)

    assert ConceptoCobro.query.filter_by(nombre='Cobro de Uniformes').first() is not None


def test_folio_de_pago_tiene_el_formato_esperado(client, app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = Cargo(
        matricula_fk=alumno.matricula_id,
        concepto='Colegiatura',
        monto=Decimal('500.00'),
        estatus=EstatusCargo.PENDIENTE,
    )
    db.session.add(cargo)
    db.session.commit()

    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    client.post(
        f'/cobro/{cargo.id}/pagar',
        data={'monto_pagado': '500.00', 'metodo_pago': 'EFECTIVO'},
        follow_redirects=True
    )

    from app import Pago
    pago = Pago.query.filter_by(cargo_fk=cargo.id).first()
    anio_actual = datetime.utcnow().year

    assert pago.folio is not None
    assert pago.folio.startswith(f'PAGO-{anio_actual}-')
