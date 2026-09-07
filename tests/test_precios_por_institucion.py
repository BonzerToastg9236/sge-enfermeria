"""
Los precios son CONFIGURACIÓN DE LA INSTITUCIÓN, no un dato que el
sistema pueda inventar.

REGLA: `ConceptoCobro.monto_sugerido` y `PlanEstudio.monto_mensualidad`
son nullable a propósito. NULL significa "esta institución todavía no
capturó ese precio" -- NO significa "$0". Son dos cosas distintas:

  * NULL  -> no hay precio configurado. El sistema NO debe cobrar nada,
             NO debe inventar un monto y NO debe generar un cargo.
  * 0.00  -> la institución decidió que ese concepto es gratuito. Es un
             precio configurado y válido, y el cargo sí se genera.

QUÉ ESTABA MAL: _generar_cargos_de_periodo() creaba el cargo con
`monto=concepto.monto_sugerido or Decimal('0.00')`. Es decir, convertía
"sin configurar" en "$0" en silencio y dejaba en el expediente del
alumno un cargo económico falso, que además nadie podía cobrar.

AHORA: si falta el precio no se genera el cargo, y se avisa
explícitamente qué configuración falta y dónde capturarla.
"""

from decimal import Decimal

from tests.conftest import crear_plan, crear_alumno, crear_usuario, login
from app import db, Cargo, ConceptoCobro, PlanEstudio, EstatusAlumno, RolUsuario


def _activar(client, alumno):
    return client.post(
        f'/alumno/{alumno.matricula_id}/cambiar-estatus',
        data={'nuevo_estatus': 'ACTIVO'},
        follow_redirects=True
    )


def _directivo(client):
    crear_usuario()
    return login(client, 'directivo1', 'clave12345')


# ---------------------------------------------------------------------------
# NULL es un estado válido del catálogo: "todavía no lo configuro"
# ---------------------------------------------------------------------------

def test_se_puede_dar_de_alta_un_concepto_sin_precio_y_queda_null(client, app):
    _directivo(client)

    client.post('/conceptos-cobro', data={'nombre': 'Constancia de Estudios'}, follow_redirects=True)

    concepto = ConceptoCobro.query.filter_by(nombre='Constancia de Estudios').first()
    assert concepto is not None, 'El catálogo debe aceptar un concepto sin precio configurado'
    assert concepto.monto_sugerido is None, 'Un precio sin capturar NO debe guardarse como 0'


def test_vaciar_el_precio_de_un_concepto_lo_deja_sin_configurar_no_en_cero(client, app):
    concepto = ConceptoCobro(nombre='Uniformes', activo=True, monto_sugerido=Decimal('900.00'))
    db.session.add(concepto)
    db.session.commit()
    _directivo(client)

    client.post(f'/conceptos-cobro/{concepto.id}/editar-precio', data={'monto_sugerido': ''}, follow_redirects=True)

    assert db.session.get(ConceptoCobro, concepto.id).monto_sugerido is None


def test_vaciar_la_mensualidad_de_una_carrera_la_deja_sin_configurar_no_en_cero(client, app):
    plan = crear_plan(nombre='Carrera con Precio', clave='CCP')
    plan.monto_mensualidad = Decimal('2800.00')
    db.session.commit()
    _directivo(client)

    client.post('/planes/mensualidades', data={'plan_id': str(plan.id), 'monto_mensualidad': ''}, follow_redirects=True)

    assert db.session.get(PlanEstudio, plan.id).monto_mensualidad is None


# ---------------------------------------------------------------------------
# Una operación que necesita un precio que no está configurado tiene que
# fallar de forma controlada: sin inventar el monto y diciéndolo claro
# ---------------------------------------------------------------------------

def test_activar_un_alumno_no_genera_cargos_en_cero_cuando_falta_el_precio(client, app):
    plan = crear_plan(nombre='Carrera sin Precios', clave='CSP')  # monto_mensualidad NULL
    db.session.add(ConceptoCobro(nombre='Inscripción', activo=True))  # monto_sugerido NULL
    db.session.commit()
    alumno = crear_alumno(plan, curp='PPPP010101HDFXYZ21', estatus=EstatusAlumno.PENDIENTE)
    _directivo(client)

    _activar(client, alumno)

    cargos = Cargo.query.filter_by(matricula_fk=alumno.matricula_id).all()
    assert cargos == [], (
        'Se generó un cargo económico con un precio que la institución nunca configuró: '
        f'{[(c.concepto, str(c.monto)) for c in cargos]}'
    )


def test_activar_un_alumno_avisa_que_falta_configurar_el_precio_del_concepto(client, app):
    plan = crear_plan(nombre='Carrera sin Precios', clave='CSP')
    db.session.add(ConceptoCobro(nombre='Inscripción', activo=True))
    db.session.commit()
    alumno = crear_alumno(plan, curp='QQQQ010101HDFXYZ22', estatus=EstatusAlumno.PENDIENTE)
    _directivo(client)

    respuesta = _activar(client, alumno)

    assert respuesta.status_code == 200
    assert 'no tiene precio configurado'.encode('utf-8') in respuesta.data, \
        'El sistema se saltó el cargo en silencio, sin decir qué configuración falta'
    assert 'Inscripción'.encode('utf-8') in respuesta.data


def test_activar_un_alumno_avisa_que_falta_la_mensualidad_de_la_carrera(client, app):
    plan = crear_plan(nombre='Carrera sin Mensualidad', clave='CSM')  # monto_mensualidad NULL
    db.session.add(ConceptoCobro(nombre='Colegiatura', activo=True, es_mensualidad=True))
    db.session.commit()
    alumno = crear_alumno(plan, curp='RRRR010101HDFXYZ23', estatus=EstatusAlumno.PENDIENTE)
    _directivo(client)

    respuesta = _activar(client, alumno)

    assert 'no tiene mensualidad configurada'.encode('utf-8') in respuesta.data
    assert Cargo.query.count() == 0


def test_avanzar_de_cuatrimestre_tampoco_genera_cargos_en_cero(client, app):
    """Misma regla en el otro camino que genera cargos automáticos."""
    plan = crear_plan(nombre='Carrera sin Precios 2', clave='CS2')
    db.session.add(ConceptoCobro(nombre='Reinscripción', activo=True))  # sin precio
    db.session.commit()
    alumno = crear_alumno(plan, curp='SSSS010101HDFXYZ24', estatus=EstatusAlumno.ACTIVO)
    _directivo(client)

    respuesta = client.post(f'/alumno/{alumno.matricula_id}/avanzar-cuatrimestre', follow_redirects=True)

    assert Cargo.query.count() == 0, 'Se generó un cargo de reinscripción con un precio inventado'
    assert 'no tiene precio configurado'.encode('utf-8') in respuesta.data


# ---------------------------------------------------------------------------
# 0.00 SÍ es un precio configurado: la institución decidió que es gratis
# ---------------------------------------------------------------------------

def test_una_mensualidad_configurada_en_cero_si_es_un_precio_valido(client, app):
    plan = crear_plan(nombre='Carrera Becada', clave='CBE')
    plan.monto_mensualidad = Decimal('0.00')  # gratuita a propósito, no "sin configurar"
    db.session.add(ConceptoCobro(nombre='Colegiatura', activo=True, es_mensualidad=True))
    db.session.commit()
    alumno = crear_alumno(plan, curp='TTTT010101HDFXYZ25', estatus=EstatusAlumno.PENDIENTE)
    _directivo(client)

    respuesta = _activar(client, alumno)

    mensualidades = Cargo.query.filter_by(concepto='Colegiatura').all()
    assert len(mensualidades) == 4, 'Un precio configurado en $0 es válido y debe generar sus cargos'
    assert all(c.monto == Decimal('0.00') for c in mensualidades)
    assert 'no tiene mensualidad configurada'.encode('utf-8') not in respuesta.data


# ---------------------------------------------------------------------------
# No regresión: con los precios capturados, todo sigue igual que antes
# ---------------------------------------------------------------------------

def test_con_los_precios_configurados_los_cargos_se_generan_igual(client, app):
    plan = crear_plan(nombre='Carrera Configurada', clave='CFG')
    plan.monto_mensualidad = Decimal('2800.00')
    db.session.add(ConceptoCobro(nombre='Inscripción', activo=True, monto_sugerido=Decimal('3500.00')))
    db.session.add(ConceptoCobro(nombre='Colegiatura', activo=True, es_mensualidad=True))
    db.session.commit()
    alumno = crear_alumno(plan, curp='UUUU010101HDFXYZ26', estatus=EstatusAlumno.PENDIENTE)
    _directivo(client)

    respuesta = _activar(client, alumno)

    inscripcion = Cargo.query.filter_by(concepto='Inscripción').all()
    mensualidades = Cargo.query.filter_by(concepto='Colegiatura').all()

    assert len(inscripcion) == 1 and inscripcion[0].monto == Decimal('3500.00')
    assert len(mensualidades) == 4 and all(c.monto == Decimal('2800.00') for c in mensualidades)
    assert 'no tiene precio configurado'.encode('utf-8') not in respuesta.data


def test_esta_correccion_no_toca_los_precios_ya_capturados(client, app):
    """
    Ningún camino de generación de cargos debe reescribir el catálogo:
    los precios solo cambian desde las pantallas de configuración.
    """
    plan = crear_plan(nombre='Carrera Intacta', clave='CIN')
    plan.monto_mensualidad = Decimal('2800.00')
    concepto = ConceptoCobro(nombre='Inscripción', activo=True, monto_sugerido=Decimal('3500.00'))
    sin_precio = ConceptoCobro(nombre='Servicio Social', activo=True)
    db.session.add_all([concepto, sin_precio])
    db.session.commit()
    alumno = crear_alumno(plan, curp='VVVV010101HDFXYZ27', estatus=EstatusAlumno.PENDIENTE)
    _directivo(client)

    _activar(client, alumno)

    assert db.session.get(ConceptoCobro, concepto.id).monto_sugerido == Decimal('3500.00')
    assert db.session.get(ConceptoCobro, sin_precio.id).monto_sugerido is None, \
        'Un precio sin configurar NO debe "rellenarse" solo'
    assert db.session.get(PlanEstudio, plan.id).monto_mensualidad == Decimal('2800.00')


# ---------------------------------------------------------------------------
# Quién puede tocar los precios de la institución
# ---------------------------------------------------------------------------

def test_un_capturador_no_puede_cambiar_el_precio_de_un_concepto(client, app):
    concepto = ConceptoCobro(nombre='Uniformes', activo=True, monto_sugerido=Decimal('900.00'))
    db.session.add(concepto)
    db.session.commit()
    crear_usuario(username='captura1', rol=RolUsuario.CAPTURADOR)
    login(client, 'captura1', 'clave12345')

    client.post(f'/conceptos-cobro/{concepto.id}/editar-precio',
                data={'monto_sugerido': '1.00'}, follow_redirects=True)

    assert db.session.get(ConceptoCobro, concepto.id).monto_sugerido == Decimal('900.00')


def test_un_capturador_no_puede_cambiar_la_mensualidad_de_una_carrera(client, app):
    plan = crear_plan(nombre='Carrera Protegida', clave='CPR')
    plan.monto_mensualidad = Decimal('2800.00')
    db.session.commit()
    crear_usuario(username='captura1', rol=RolUsuario.CAPTURADOR)
    login(client, 'captura1', 'clave12345')

    client.post('/planes/mensualidades',
                data={'plan_id': str(plan.id), 'monto_mensualidad': '1.00'}, follow_redirects=True)

    assert db.session.get(PlanEstudio, plan.id).monto_mensualidad == Decimal('2800.00')


def test_un_contador_no_puede_cambiar_la_mensualidad_de_una_carrera(client, app):
    """El precio por carrera lo define Dirección; Contaduría solo cobra con él."""
    plan = crear_plan(nombre='Carrera Protegida 2', clave='CP2')
    plan.monto_mensualidad = Decimal('2800.00')
    db.session.commit()
    crear_usuario(username='conta1', rol=RolUsuario.CONTADOR)
    login(client, 'conta1', 'clave12345')

    client.post('/planes/mensualidades',
                data={'plan_id': str(plan.id), 'monto_mensualidad': '1.00'}, follow_redirects=True)

    assert db.session.get(PlanEstudio, plan.id).monto_mensualidad == Decimal('2800.00')


def test_sin_sesion_no_se_puede_consultar_ni_cambiar_un_precio(client, app):
    plan = crear_plan(nombre='Carrera Cerrada', clave='CCE')
    plan.monto_mensualidad = Decimal('2800.00')
    db.session.commit()

    consulta = client.get('/planes/mensualidades', follow_redirects=False)
    cambio = client.post('/planes/mensualidades',
                         data={'plan_id': str(plan.id), 'monto_mensualidad': '1.00'},
                         follow_redirects=False)

    assert consulta.status_code == 302 and '/login' in consulta.headers['Location']
    assert cambio.status_code == 302
    assert db.session.get(PlanEstudio, plan.id).monto_mensualidad == Decimal('2800.00')
