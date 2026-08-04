"""
Pruebas del Sistema de Cobros: cálculo de saldo (siempre derivado de los
pagos reales, nunca guardado a mano), y permisos por rol — Administrativo
puede consultar y registrar pagos, pero SOLO Directivo puede crear o
cancelar cargos.
"""

from decimal import Decimal

from tests.conftest import crear_plan, crear_alumno, crear_usuario, login
from app import db, Cargo, EstatusCargo, RolUsuario, ConceptoCobro, EstatusAlumno, Alumno, Pago


def _crear_cargo(alumno, concepto='Colegiatura de Prueba', monto='1500.00'):
    cargo = Cargo(
        matricula_fk=alumno.matricula_id,
        concepto=concepto,
        monto=Decimal(monto),
        estatus=EstatusCargo.PENDIENTE,
    )
    db.session.add(cargo)
    db.session.commit()
    return cargo


def _crear_concepto(nombre='Colegiatura', activo=True):
    """
    Crea un ConceptoCobro del catálogo. La ruta /alumno/<matricula>/cobros/nuevo
    ya no acepta texto libre en 'concepto' -- exige un 'concepto_cobro_id' real
    que exista (y esté activo) en el catálogo.
    """
    concepto = ConceptoCobro(nombre=nombre, activo=activo)
    db.session.add(concepto)
    db.session.commit()
    return concepto


def test_cargo_recien_creado_tiene_saldo_igual_al_monto(app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo(alumno)

    assert cargo.saldo_pendiente() == Decimal('1500.00')
    assert cargo.total_pagado() == Decimal('0.00')
    assert cargo.estatus == EstatusCargo.PENDIENTE


def test_pago_parcial_deja_estatus_parcial_y_saldo_correcto(client, app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo(alumno)
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    client.post(
        f'/cobro/{cargo.id}/pagar',
        data={'monto_pagado': '500.00', 'metodo_pago': 'EFECTIVO'},
        follow_redirects=True
    )

    cargo_actualizado = Cargo.query.get(cargo.id)
    assert cargo_actualizado.total_pagado() == Decimal('500.00')
    assert cargo_actualizado.saldo_pendiente() == Decimal('1000.00')
    assert cargo_actualizado.estatus == EstatusCargo.PARCIAL


def test_pago_completo_deja_estatus_pagado(client, app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo(alumno, monto='1000.00')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    client.post(
        f'/cobro/{cargo.id}/pagar',
        data={'monto_pagado': '1000.00', 'metodo_pago': 'TRANSFERENCIA'},
        follow_redirects=True
    )

    cargo_actualizado = Cargo.query.get(cargo.id)
    assert cargo_actualizado.saldo_pendiente() == Decimal('0.00')
    assert cargo_actualizado.estatus == EstatusCargo.PAGADO


def test_no_se_puede_pagar_mas_del_saldo_pendiente(client, app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo(alumno, monto='1000.00')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    client.post(
        f'/cobro/{cargo.id}/pagar',
        data={'monto_pagado': '9999.00', 'metodo_pago': 'EFECTIVO'},
        follow_redirects=True
    )

    cargo_actualizado = Cargo.query.get(cargo.id)
    assert cargo_actualizado.total_pagado() == Decimal('0.00')  # el pago NO se registró
    assert cargo_actualizado.estatus == EstatusCargo.PENDIENTE


def test_administrativo_puede_registrar_pagos(client, app):
    """Regla de negocio: Administrativo SÍ puede actualizar (registrar pagos)."""
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo(alumno, monto='500.00')
    crear_usuario(username='admin1', password='clave12345', rol=RolUsuario.ADMINISTRATIVO)
    login(client, 'admin1', 'clave12345')

    client.post(
        f'/cobro/{cargo.id}/pagar',
        data={'monto_pagado': '500.00', 'metodo_pago': 'EFECTIVO'},
        follow_redirects=True
    )

    cargo_actualizado = Cargo.query.get(cargo.id)
    assert cargo_actualizado.estatus == EstatusCargo.PAGADO


def test_administrativo_no_puede_crear_cargos(client, app):
    """Regla de negocio: SOLO Directivo define la estructura de cobros."""
    plan = crear_plan()
    alumno = crear_alumno(plan)
    concepto = _crear_concepto(nombre='Colegiatura no autorizada')
    crear_usuario(username='admin1', password='clave12345', rol=RolUsuario.ADMINISTRATIVO)
    login(client, 'admin1', 'clave12345')

    respuesta = client.post(
        f'/alumno/{alumno.matricula_id}/cobros/nuevo',
        data={'concepto_cobro_id': str(concepto.id), 'monto': '1000.00'},
        follow_redirects=True
    )

    assert respuesta.status_code == 200
    assert 'permisos'.encode('utf-8') in respuesta.data.lower()
    assert Cargo.query.filter_by(matricula_fk=alumno.matricula_id).count() == 0


def test_administrativo_no_puede_cancelar_cargos(client, app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo(alumno)
    crear_usuario(username='admin1', password='clave12345', rol=RolUsuario.ADMINISTRATIVO)
    login(client, 'admin1', 'clave12345')

    respuesta = client.post(f'/cobro/{cargo.id}/cancelar', follow_redirects=True)

    assert respuesta.status_code == 200
    assert 'permisos'.encode('utf-8') in respuesta.data.lower()

    cargo_actualizado = Cargo.query.get(cargo.id)
    assert cargo_actualizado.estatus != EstatusCargo.CANCELADO


def test_directivo_si_puede_crear_y_cancelar_cargos(client, app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    concepto = _crear_concepto(nombre='Inscripción')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    client.post(
        f'/alumno/{alumno.matricula_id}/cobros/nuevo',
        data={'concepto_cobro_id': str(concepto.id), 'monto': '2000.00'},
        follow_redirects=True
    )

    cargo = Cargo.query.filter_by(matricula_fk=alumno.matricula_id).first()
    assert cargo is not None
    assert cargo.monto == Decimal('2000.00')

    client.post(f'/cobro/{cargo.id}/cancelar', data={'comentario': 'Duplicado'}, follow_redirects=True)

    cargo_actualizado = Cargo.query.get(cargo.id)
    assert cargo_actualizado.estatus == EstatusCargo.CANCELADO


def test_activar_alumno_genera_cargos_de_inscripcion_automaticamente(client, app):
    """
    Al pasar un alumno de Pendiente a Activo POR PRIMERA VEZ, debe
    generarse automáticamente: 1 cargo de Inscripción + un cargo de
    mensualidad por cada mes del cuatrimestre en curso (usando el precio
    de Mensualidades por Carrera de su plan).
    """
    plan = crear_plan(nombre='Carrera con Mensualidad', clave='CCM')
    plan.monto_mensualidad = Decimal('1800.00')
    db.session.add(ConceptoCobro(nombre='Inscripción', activo=True, monto_sugerido=Decimal('3500.00')))
    db.session.add(ConceptoCobro(nombre='Colegiatura', activo=True, es_mensualidad=True))
    db.session.commit()

    alumno = crear_alumno(plan, curp='GGGG010101HDFXYZ07', estatus=EstatusAlumno.PENDIENTE)
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    client.post(
        f'/alumno/{alumno.matricula_id}/cambiar-estatus',
        data={'nuevo_estatus': 'ACTIVO'},
        follow_redirects=True
    )

    cargos = Cargo.query.filter_by(matricula_fk=alumno.matricula_id).all()
    cargos_inscripcion = [c for c in cargos if c.concepto == 'Inscripción']
    cargos_mensualidad = [c for c in cargos if c.concepto == 'Colegiatura']

    assert len(cargos_inscripcion) == 1
    assert cargos_inscripcion[0].monto == Decimal('3500.00')

    assert len(cargos_mensualidad) == 4  # un cuatrimestre = 4 meses
    for cargo in cargos_mensualidad:
        assert cargo.monto == Decimal('1800.00')
        assert cargo.fecha_vencimiento.day == 10


def test_activar_alumno_dos_veces_no_duplica_cargos(client, app):
    """Si el alumno ya estaba Activo antes (fecha_validacion ya existe), no se vuelven a generar cargos."""
    plan = crear_plan(nombre='Carrera Reactivada', clave='CRA')
    plan.monto_mensualidad = Decimal('1000.00')
    db.session.add(ConceptoCobro(nombre='Inscripción', activo=True, monto_sugerido=Decimal('2000.00')))
    db.session.commit()

    alumno = crear_alumno(plan, curp='HHHH010101HDFXYZ08', estatus=EstatusAlumno.ACTIVO)
    from datetime import datetime as dt
    alumno.fecha_validacion = dt.utcnow()  # ya había sido validado antes
    db.session.commit()

    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    # Lo mandamos a Baja Temporal y lo regresamos a Activo
    client.post(f'/alumno/{alumno.matricula_id}/cambiar-estatus', data={'nuevo_estatus': 'BAJA_TEMPORAL'}, follow_redirects=True)
    client.post(f'/alumno/{alumno.matricula_id}/cambiar-estatus', data={'nuevo_estatus': 'ACTIVO'}, follow_redirects=True)

    cargos = Cargo.query.filter_by(matricula_fk=alumno.matricula_id).all()
    assert len(cargos) == 0  # nunca se generó nada porque ya tenía fecha_validacion de antes


def test_activar_alumno_genera_carga_academica_respetando_el_escudo(client, app):
    """
    Al activar por primera vez, se le debe generar InscripcionMateria SOLO
    para las materias de SU plan y SU cuatrimestre -- nunca de otra carrera.
    """
    from app import InscripcionMateria, Materia

    plan_alumno = crear_plan(nombre='Carrera del Alumno', clave='CDA')
    plan_ajeno = crear_plan(nombre='Otra Carrera', clave='OTC')

    materia_1 = Materia(nombre='Materia Uno', cuatrimestre=1, id_plan_fk=plan_alumno.id)
    materia_2 = Materia(nombre='Materia Dos', cuatrimestre=1, id_plan_fk=plan_alumno.id)
    materia_otro_cuatri = Materia(nombre='Materia de Otro Cuatri', cuatrimestre=2, id_plan_fk=plan_alumno.id)
    materia_otra_carrera = Materia(nombre='Materia Ajena', cuatrimestre=1, id_plan_fk=plan_ajeno.id)
    db.session.add_all([materia_1, materia_2, materia_otro_cuatri, materia_otra_carrera])
    db.session.commit()

    alumno = crear_alumno(plan_alumno, curp='IIII010101HDFXYZ09', estatus=EstatusAlumno.PENDIENTE)
    alumno.cuatrimestre_actual = 1
    db.session.commit()

    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    client.post(
        f'/alumno/{alumno.matricula_id}/cambiar-estatus',
        data={'nuevo_estatus': 'ACTIVO'},
        follow_redirects=True
    )

    inscripciones = InscripcionMateria.query.filter_by(matricula_fk=alumno.matricula_id).all()
    nombres_inscritos = {i.materia.nombre for i in inscripciones}

    assert nombres_inscritos == {'Materia Uno', 'Materia Dos'}  # solo su plan, solo su cuatrimestre


def test_avanzar_cuatrimestre_individual_genera_reinscripcion_y_mensualidades(client, app):
    """Avanzar a un alumno debe: subir cuatrimestre_actual, generar Reinscripción + mensualidades + carga académica nueva."""
    from app import Materia, InscripcionMateria

    plan = crear_plan(nombre='Carrera Avance', clave='CAV')
    plan.monto_mensualidad = Decimal('1200.00')
    db.session.add(ConceptoCobro(nombre='Reinscripción', activo=True, monto_sugerido=Decimal('800.00')))
    db.session.add(ConceptoCobro(nombre='Colegiatura', activo=True, es_mensualidad=True))
    db.session.add(Materia(nombre='Materia del Cuatri 2', cuatrimestre=2, id_plan_fk=plan.id))
    db.session.commit()

    alumno = crear_alumno(plan, curp='KKKK010101HDFXYZ11', estatus=EstatusAlumno.ACTIVO)
    alumno.cuatrimestre_actual = 1
    db.session.commit()

    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    client.post(f'/alumno/{alumno.matricula_id}/avanzar-cuatrimestre', follow_redirects=True)

    alumno_actualizado = db.session.get(Alumno, alumno.matricula_id)
    assert alumno_actualizado.cuatrimestre_actual == 2

    cargos = Cargo.query.filter_by(matricula_fk=alumno.matricula_id).all()
    assert any(c.concepto == 'Reinscripción' for c in cargos)
    assert any(c.concepto == 'Colegiatura' for c in cargos)

    carga_academica = InscripcionMateria.query.filter_by(matricula_fk=alumno.matricula_id).all()
    assert any(i.materia.nombre == 'Materia del Cuatri 2' for i in carga_academica)


def test_no_avanza_alumno_en_ultimo_cuatrimestre(client, app):
    from app import app as flask_app
    plan = crear_plan(nombre='Carrera Tope', clave='CTP')
    alumno = crear_alumno(plan, curp='LLLL010101HDFXYZ12', estatus=EstatusAlumno.ACTIVO)
    alumno.cuatrimestre_actual = flask_app.config['CUATRIMESTRES_MAXIMOS']
    db.session.commit()

    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    client.post(f'/alumno/{alumno.matricula_id}/avanzar-cuatrimestre', follow_redirects=True)

    alumno_actualizado = db.session.get(Alumno, alumno.matricula_id)
    assert alumno_actualizado.cuatrimestre_actual == flask_app.config['CUATRIMESTRES_MAXIMOS']  # no cambió


def test_avanzar_cuatrimestre_en_lote_solo_toma_activos_del_cuatrimestre_indicado(client, app):
    plan = crear_plan(nombre='Carrera Lote', clave='CLT')
    plan.monto_mensualidad = Decimal('1000.00')
    db.session.commit()

    alumno_activo_cuatri1 = crear_alumno(plan, curp='MMMM010101HDFXYZ13', estatus=EstatusAlumno.ACTIVO, matricula_id='CLT2026-00001')
    alumno_activo_cuatri1.cuatrimestre_actual = 1
    alumno_pendiente_cuatri1 = crear_alumno(plan, curp='NNNN010101HDFXYZ14', estatus=EstatusAlumno.PENDIENTE, matricula_id='CLT2026-00002')
    alumno_pendiente_cuatri1.cuatrimestre_actual = 1
    db.session.commit()

    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    client.post(
        '/alumnos/avanzar-cuatrimestre-lote',
        data={'plan_id': str(plan.id), 'cuatrimestre_actual': '1'},
        follow_redirects=True
    )

    pass  # Alumno ya importado arriba
    activo_actualizado = db.session.get(Alumno, alumno_activo_cuatri1.matricula_id)
    pendiente_actualizado = db.session.get(Alumno, alumno_pendiente_cuatri1.matricula_id)

    assert activo_actualizado.cuatrimestre_actual == 2  # sí avanzó
    assert pendiente_actualizado.cuatrimestre_actual == 1  # no avanzó, no está Activo


def test_anular_pago_excluye_del_saldo_y_del_estatus(client, app):
    """
    Escenario exacto planteado: el contador captura un pago, se equivoca,
    lo anula -- el saldo pendiente del cargo debe regresar a como estaba
    antes, y el cargo debe dejar de verse como Pagado.
    """
    plan = crear_plan()
    alumno = crear_alumno(plan, curp='KKKK010101HDFXYZ11')
    cargo = _crear_cargo(alumno, concepto='Colegiatura', monto='1000.00')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    # Se captura el pago completo por error de monto (o de alumno, etc.)
    client.post(
        f'/cobro/{cargo.id}/pagar',
        data={'monto_pagado': '1000.00', 'metodo_pago': 'EFECTIVO'},
        follow_redirects=True
    )

    cargo_actualizado = Cargo.query.get(cargo.id)
    assert cargo_actualizado.estatus == EstatusCargo.PAGADO
    pago = cargo_actualizado.pagos[0]

    # El contador se da cuenta del error y anula el pago
    client.post(
        f'/pago/{pago.id}/anular',
        data={'motivo_anulacion': 'Monto capturado por error, era de otro alumno'},
        follow_redirects=True
    )

    cargo_tras_anular = Cargo.query.get(cargo.id)
    pago_tras_anular = Pago.query.get(pago.id)

    assert pago_tras_anular.anulado is True
    assert pago_tras_anular.motivo_anulacion == 'Monto capturado por error, era de otro alumno'
    assert cargo_tras_anular.estatus == EstatusCargo.PENDIENTE  # regresó a como estaba antes
    assert cargo_tras_anular.saldo_pendiente() == Decimal('1000.00')


def test_no_se_puede_anular_un_pago_sin_motivo(client, app):
    plan = crear_plan()
    alumno = crear_alumno(plan, curp='LLLL010101HDFXYZ12')
    cargo = _crear_cargo(alumno, concepto='Colegiatura', monto='500.00')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    client.post(f'/cobro/{cargo.id}/pagar', data={'monto_pagado': '500.00', 'metodo_pago': 'EFECTIVO'}, follow_redirects=True)
    pago = Cargo.query.get(cargo.id).pagos[0]

    client.post(f'/pago/{pago.id}/anular', data={'motivo_anulacion': 'x'}, follow_redirects=True)

    pago_sin_cambios = Pago.query.get(pago.id)
    assert pago_sin_cambios.anulado is False  # se rechazó por motivo demasiado corto


def test_administrativo_no_puede_anular_pagos(client, app):
    """Administrativo puede registrar pagos, pero anular es exclusivo de Directivo/Contador."""
    plan = crear_plan()
    alumno = crear_alumno(plan, curp='MMMM010101HDFXYZ13')
    cargo = _crear_cargo(alumno, concepto='Colegiatura', monto='800.00')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')
    client.post(f'/cobro/{cargo.id}/pagar', data={'monto_pagado': '800.00', 'metodo_pago': 'EFECTIVO'}, follow_redirects=True)
    pago = Cargo.query.get(cargo.id).pagos[0]
    client.get('/logout')

    crear_usuario(username='admin1', rol=RolUsuario.ADMINISTRATIVO)
    login(client, 'admin1', 'clave12345')

    respuesta = client.post(f'/pago/{pago.id}/anular', data={'motivo_anulacion': 'Intento no autorizado'}, follow_redirects=True)

    assert respuesta.status_code in (403, 200)  # según cómo maneje rol_requerido el redireccionamiento/])
    pago_sin_cambios = Pago.query.get(pago.id)
    assert pago_sin_cambios.anulado is False


def test_beca_de_porcentaje_reduce_el_monto_de_mensualidad_al_avanzar_cuatrimestre(client, app):
    """Una beca de 20% otorgada ANTES de generar mensualidades debe reflejarse en los cargos nuevos."""
    plan = crear_plan(nombre='Carrera con Becas', clave='CCB')
    plan.monto_mensualidad = Decimal('2000.00')
    db.session.add(ConceptoCobro(nombre='Reinscripción', activo=True, monto_sugerido=Decimal('1000.00')))
    db.session.add(ConceptoCobro(nombre='Colegiatura', activo=True, es_mensualidad=True))
    db.session.commit()

    alumno = crear_alumno(plan, curp='NNNN010101HDFXYZ14', estatus=EstatusAlumno.ACTIVO)
    alumno.cuatrimestre_actual = 1
    db.session.commit()

    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    from app import periodo_escolar_actual
    periodo_actual = periodo_escolar_actual()

    client.post(
        f'/alumno/{alumno.matricula_id}/becas',
        data={
            'nombre': 'Beca Excelencia',
            'tipo_descuento': 'PORCENTAJE',
            'valor': '20',
            'periodo_escolar': periodo_actual,
        },
        follow_redirects=True
    )

    # Avanza de cuatrimestre para que se generen mensualidades nuevas, ya con la beca activa
    client.post(f'/alumno/{alumno.matricula_id}/avanzar-cuatrimestre', follow_redirects=True)

    cargos_mensualidad = Cargo.query.filter_by(matricula_fk=alumno.matricula_id, concepto='Colegiatura').all()
    assert len(cargos_mensualidad) == 4
    for cargo in cargos_mensualidad:
        assert cargo.monto == Decimal('1600.00')  # 2000 - 20% = 1600


def test_beca_ajusta_cargos_de_mensualidad_ya_generados_sin_pagos(client, app):
    """Si la beca se otorga DESPUÉS de generar mensualidades, los cargos que aún no tienen pago se ajustan solos."""
    plan = crear_plan(nombre='Carrera Beca Tardía', clave='CBT')
    plan.monto_mensualidad = Decimal('1000.00')
    db.session.add(ConceptoCobro(nombre='Inscripción', activo=True, monto_sugerido=Decimal('500.00')))
    db.session.add(ConceptoCobro(nombre='Colegiatura', activo=True, es_mensualidad=True))
    db.session.commit()

    alumno = crear_alumno(plan, curp='OOOO010101HDFXYZ15', estatus=EstatusAlumno.PENDIENTE)
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    # Activar genera las mensualidades SIN beca todavía (precio completo)
    client.post(f'/alumno/{alumno.matricula_id}/cambiar-estatus', data={'nuevo_estatus': 'ACTIVO'}, follow_redirects=True)

    cargo_antes = Cargo.query.filter_by(matricula_fk=alumno.matricula_id, concepto='Colegiatura').first()
    assert cargo_antes.monto == Decimal('1000.00')

    from app import periodo_escolar_actual
    periodo_actual = periodo_escolar_actual()

    # Ahora se otorga la beca -- debe ajustar los cargos ya generados que no tienen pago
    client.post(
        f'/alumno/{alumno.matricula_id}/becas',
        data={
            'nombre': 'Beca Tardía',
            'tipo_descuento': 'MONTO_FIJO',
            'valor': '300',
            'periodo_escolar': periodo_actual,
        },
        follow_redirects=True
    )

    cargo_despues = Cargo.query.get(cargo_antes.id)
    assert cargo_despues.monto == Decimal('700.00')  # 1000 - 300


def test_beca_no_ajusta_cargo_que_ya_tiene_un_pago(client, app):
    """Un cargo con un pago (aunque sea parcial) nunca se toca al otorgar una beca -- se queda igual."""
    plan = crear_plan(nombre='Carrera Beca Sin Tocar Pagos', clave='CSP')
    plan.monto_mensualidad = Decimal('1000.00')
    db.session.add(ConceptoCobro(nombre='Colegiatura', activo=True, es_mensualidad=True))
    db.session.commit()

    alumno = crear_alumno(plan, curp='PPPP010101HDFXYZ16', estatus=EstatusAlumno.PENDIENTE)
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    client.post(f'/alumno/{alumno.matricula_id}/cambiar-estatus', data={'nuevo_estatus': 'ACTIVO'}, follow_redirects=True)
    cargo = Cargo.query.filter_by(matricula_fk=alumno.matricula_id, concepto='Colegiatura').first()

    # Se le hace un pago parcial ANTES de que exista la beca
    client.post(f'/cobro/{cargo.id}/pagar', data={'monto_pagado': '200.00', 'metodo_pago': 'EFECTIVO'}, follow_redirects=True)

    from app import periodo_escolar_actual
    client.post(
        f'/alumno/{alumno.matricula_id}/becas',
        data={
            'nombre': 'Beca Tardía Con Pago',
            'tipo_descuento': 'MONTO_FIJO',
            'valor': '300',
            'periodo_escolar': periodo_escolar_actual(),
        },
        follow_redirects=True
    )

    cargo_sin_tocar = Cargo.query.get(cargo.id)
    assert cargo_sin_tocar.monto == Decimal('1000.00')  # NUNCA se ajusta si ya tiene un pago
