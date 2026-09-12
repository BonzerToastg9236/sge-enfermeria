"""
Sistema de cobros: cargos, pagos, becas, estado de cuenta, generación
masiva de mensualidades y recordatorios de vencimiento.

registrar_pago() contiene el fix #2 de la auditoría de producción: un
SELECT ... FOR UPDATE sobre Cargo (con with_for_update(), gateado por
db.engine.dialect.name != 'sqlite' porque SQLite no soporta bloqueo por
fila) que impide que dos cobros concurrentes sobre el mismo cargo se
pasen del saldo disponible. Ver
deploy/PENDIENTES_PRODUCCION.md §1 para el detalle verificado del
mecanismo. Esta función se mueve sin editar una sola línea de su cuerpo.
"""

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from flask import Blueprint, render_template, request, flash, redirect, url_for, abort
from flask_login import current_user
from sqlalchemy.exc import IntegrityError

from extensiones import db
from modelos import (
    Cargo, EstatusCargo, Pago, Beca, ConceptoCobro, MetodoPago, Alumno,
    EstatusAlumno, TipoDescuentoBeca,
)
from utilidades.seguridad import rol_requerido
from utilidades.fechas import hoy_local, ahora_utc, periodo_escolar_actual
from servicios.cobros import _cargo_duplicado, _vencimiento_dia_10_sugerido, _monto_mensualidad_con_beca
from servicios.correo import enviar_comprobante_pago, enviar_recordatorio_vencimiento, DIAS_AVISO_VENCIMIENTO

cobros_bp = Blueprint('cobros', __name__)


@cobros_bp.route('/alumno/<matricula>/cobros')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')
def cobros(matricula):
    alumno = db.get_or_404(Alumno, matricula)
    cargos = Cargo.query.filter_by(matricula_fk=matricula).order_by(Cargo.fecha_generacion.desc()).all()

    # Recargos "automáticos": se recalculan cada vez que se consulta la
    # pantalla, usando la configuración VIGENTE (auto-ajustable). No
    # depende de ningún cron job en segundo plano.
    hubo_cambios = False
    for cargo in cargos:
        recargo_antes = cargo.recargo_aplicado
        cargo.actualizar_recargo_si_vencido()
        if cargo.recargo_aplicado != recargo_antes:
            cargo.actualizar_estatus()
            hubo_cambios = True
    if hubo_cambios:
        db.session.commit()

    total_adeudado = sum(
        (c.saldo_pendiente() for c in cargos if c.estatus != EstatusCargo.CANCELADO),
        Decimal('0.00')
    )

    return render_template(
        'cobros.html',
        alumno=alumno,
        cargos=cargos,
        total_adeudado=total_adeudado,
        metodos_pago=list(MetodoPago),
        conceptos_cobro=ConceptoCobro.query.filter_by(activo=True).order_by(ConceptoCobro.nombre.asc()).all(),
        periodo_escolar_sugerido=periodo_escolar_actual(),
    )


@cobros_bp.route('/alumno/<matricula>/becas', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def becas_alumno(matricula):
    """
    Otorgar/consultar becas y descuentos de un alumno -- aplican
    ÚNICAMENTE a Colegiatura/Mensualidad, nunca a otros conceptos. Si ya
    existían cargos de mensualidad generados para el periodo de la beca
    y todavía no tienen ningún pago, se les ajusta el monto de una vez
    (para no obligar a cancelar y recrear a mano).
    """
    alumno = db.get_or_404(Alumno, matricula)

    if request.method == 'POST':
        nombre = request.form.get('nombre', '').strip()
        tipo_descuento_raw = request.form.get('tipo_descuento', '').strip().upper()
        valor_raw = request.form.get('valor', '').strip()
        periodo_escolar = request.form.get('periodo_escolar', '').strip()
        motivo = request.form.get('motivo', '').strip() or None

        errores = []
        if len(nombre) < 3:
            errores.append('El nombre de la beca debe tener al menos 3 caracteres.')
        if tipo_descuento_raw not in TipoDescuentoBeca.__members__:
            errores.append('Selecciona el tipo de descuento (porcentaje o monto fijo).')
        if not periodo_escolar:
            errores.append('Indica el periodo escolar de vigencia (ej. "2026-B").')

        valor = None
        try:
            valor = Decimal(valor_raw)
            if valor <= 0:
                raise InvalidOperation
            if tipo_descuento_raw == 'PORCENTAJE' and valor > 100:
                errores.append('El porcentaje no puede ser mayor a 100.')
        except InvalidOperation:
            errores.append('Indica un valor de descuento válido.')

        if errores:
            for error in errores:
                flash(error, 'danger')
            return redirect(url_for('cobros.becas_alumno', matricula=matricula))

        beca = Beca(
            matricula_fk=alumno.matricula_id,
            nombre=nombre,
            tipo_descuento=TipoDescuentoBeca[tipo_descuento_raw],
            valor=valor,
            periodo_escolar=periodo_escolar,
            otorgada_por_fk=current_user.id,
            motivo=motivo,
        )
        db.session.add(beca)
        db.session.flush()

        # Ajusta cargos de mensualidad YA generados para este periodo que
        # aún no tienen ningún pago -- nunca se toca uno que ya tenga un
        # pago encima, aunque sea parcial.
        cargos_ajustados = 0
        concepto_mensualidad = ConceptoCobro.query.filter_by(es_mensualidad=True, activo=True).first()
        if concepto_mensualidad:
            candidatos = Cargo.query.filter(
                Cargo.matricula_fk == alumno.matricula_id,
                Cargo.concepto == concepto_mensualidad.nombre,
                Cargo.periodo_escolar.like(f'{periodo_escolar}%'),
                Cargo.estatus == EstatusCargo.PENDIENTE,
            ).all()
            for cargo in candidatos:
                if cargo.total_pagado() == 0:
                    nuevo_monto = _monto_mensualidad_con_beca(alumno, cargo.periodo_escolar)
                    if nuevo_monto is not None:
                        cargo.monto = nuevo_monto
                        cargos_ajustados += 1

        db.session.commit()

        flash(f'Beca "{nombre}" otorgada para el periodo {periodo_escolar}.', 'success')
        if cargos_ajustados:
            flash(f'{cargos_ajustados} cargo(s) de mensualidad ya generados se ajustaron con el nuevo descuento.', 'success')

        return redirect(url_for('cobros.becas_alumno', matricula=matricula))

    becas = Beca.query.filter_by(matricula_fk=matricula).order_by(Beca.fecha_otorgada.desc()).all()
    return render_template(
        'becas_alumno.html',
        alumno=alumno,
        becas=becas,
        tipos_descuento=list(TipoDescuentoBeca),
        periodo_escolar_sugerido=periodo_escolar_actual(),
    )


@cobros_bp.route('/becas/<int:beca_id>/desactivar', methods=['POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def desactivar_beca(beca_id):
    """Desactiva una beca -- los cargos que ya se generaron con el descuento NO se revierten automáticamente."""
    beca = db.get_or_404(Beca, beca_id)
    beca.activa = False
    db.session.commit()
    flash(f'Beca "{beca.nombre}" desactivada. Los cargos ya generados con ese descuento no se revierten solos.', 'warning')
    return redirect(url_for('cobros.becas_alumno', matricula=beca.matricula_fk))


@cobros_bp.route('/alumno/<matricula>/cobros/nuevo', methods=['POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def nuevo_cargo(matricula):
    alumno = db.get_or_404(Alumno, matricula)

    concepto_cobro_id_raw = request.form.get('concepto_cobro_id', '').strip()
    monto_raw = request.form.get('monto', '').strip()
    periodo_escolar = request.form.get('periodo_escolar', '').strip() or None
    fecha_vencimiento_raw = request.form.get('fecha_vencimiento', '').strip()

    errores = []

    concepto_cobro = None
    if not concepto_cobro_id_raw:
        errores.append('Selecciona un concepto del catálogo.')
    else:
        try:
            concepto_cobro = db.session.get(ConceptoCobro, int(concepto_cobro_id_raw))
        except (ValueError, TypeError):
            concepto_cobro = None
        if not concepto_cobro or not concepto_cobro.activo:
            errores.append('El concepto seleccionado no es válido.')

    monto = None
    try:
        monto = Decimal(monto_raw)
        if monto <= 0:
            errores.append('El monto debe ser mayor a 0.')
    except InvalidOperation:
        errores.append('El monto no es un número válido.')

    fecha_vencimiento = None
    if fecha_vencimiento_raw:
        try:
            fecha_vencimiento = datetime.strptime(fecha_vencimiento_raw, '%Y-%m-%d').date()
        except ValueError:
            errores.append('La fecha de vencimiento no es válida.')

    if errores:
        for error in errores:
            flash(error, 'danger')
        return redirect(url_for('cobros.cobros', matricula=matricula))

    duplicado = _cargo_duplicado(alumno.matricula_id, concepto_cobro.id, periodo_escolar)
    if duplicado:
        flash(
            f'Ya existe un cargo de "{concepto_cobro.nombre}"'
            f'{" para el periodo " + periodo_escolar if periodo_escolar else ""} '
            f'sin cancelar (estatus: {duplicado.estatus.value}, folio interno #{duplicado.id}). '
            'Si de verdad necesitas otro, cancela primero el existente o usa un periodo distinto.',
            'warning'
        )
        return redirect(url_for('cobros.cobros', matricula=matricula))

    nuevo = Cargo(
        matricula_fk=alumno.matricula_id,
        concepto_cobro_fk=concepto_cobro.id,
        concepto=concepto_cobro.nombre,  # Denormalizado para mostrar sin necesidad de join
        monto=monto,
        periodo_escolar=periodo_escolar,
        fecha_vencimiento=fecha_vencimiento,
        generado_por_fk=current_user.id,
    )
    db.session.add(nuevo)
    try:
        db.session.commit()
    except IntegrityError:
        # CONCURRENCIA: _cargo_duplicado() de arriba ya no es la única
        # protección -- el índice único de la BD (migración b7e2c9a41f3d)
        # es la garantía real. Esto es lo que atrapa el caso donde dos
        # peticiones pasaron el check en Python casi al mismo tiempo (ej.
        # doble clic, o dos personas de ventanilla capturando el mismo
        # cargo) y solo una puede ganar la carrera del INSERT.
        db.session.rollback()
        flash(
            f'Ya existe un cargo de "{concepto_cobro.nombre}"'
            f'{" para el periodo " + periodo_escolar if periodo_escolar else ""} '
            'sin cancelar -- se generó justo ahora, probablemente por un doble '
            'clic o dos personas capturando al mismo tiempo. No se creó otro.',
            'warning'
        )
        return redirect(url_for('cobros.cobros', matricula=matricula))

    flash(f'Cargo "{concepto_cobro.nombre}" agregado correctamente.', 'success')
    return redirect(url_for('cobros.cobros', matricula=matricula))


@cobros_bp.route('/cobro/<int:cargo_id>/pagar', methods=['POST'])
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')
def registrar_pago(cargo_id):
    # CONCURRENCIA: el saldo (monto + recargo_aplicado - pagos) vive en la
    # fila Cargo, así que es ESA fila la que hay que bloquear -- no la de
    # Pago, que todavía no existe en este punto. with_for_update() obliga
    # a que una segunda petición sobre el MISMO cargo espere a que esta
    # transacción termine (commit incluido) antes de poder leer su propio
    # saldo, así que lo ve ya actualizado en vez de leer el mismo saldo
    # "viejo" que esta transacción. Mismo patrón que generar_matricula()/
    # siguiente_folio() (ver esas funciones): with_for_update() solo bloquea
    # de verdad en motores que lo soportan (PostgreSQL, producción); en
    # SQLite (desarrollo) se ignora silenciosamente, no hay bloqueo por fila.
    consulta_cargo = Cargo.query.filter_by(id=cargo_id)
    if db.engine.dialect.name != 'sqlite':
        consulta_cargo = consulta_cargo.with_for_update()
    cargo = consulta_cargo.first()
    if cargo is None:
        abort(404)

    if cargo.estatus == EstatusCargo.CANCELADO:
        flash('Este cargo está cancelado; no se le pueden registrar pagos.', 'danger')
        return redirect(url_for('cobros.cobros', matricula=cargo.matricula_fk))

    monto_raw = request.form.get('monto_pagado', '').strip()
    metodo_raw = request.form.get('metodo_pago', 'EFECTIVO').upper()
    referencia = request.form.get('referencia', '').strip() or None
    comentario = request.form.get('comentario', '').strip() or None

    try:
        monto_pagado = Decimal(monto_raw)
        if monto_pagado <= 0:
            raise InvalidOperation()
    except InvalidOperation:
        flash('El monto pagado no es válido.', 'danger')
        return redirect(url_for('cobros.cobros', matricula=cargo.matricula_fk))

    saldo = cargo.saldo_pendiente()
    if monto_pagado > saldo:
        flash(
            f'El monto pagado (${monto_pagado}) es mayor al saldo pendiente (${saldo}). '
            'Verifica el monto.',
            'danger'
        )
        return redirect(url_for('cobros.cobros', matricula=cargo.matricula_fk))

    if metodo_raw not in MetodoPago.__members__:
        metodo_raw = 'EFECTIVO'

    pago = Pago(
        cargo=cargo,
        monto_pagado=monto_pagado,
        metodo_pago=MetodoPago[metodo_raw],
        referencia=referencia,
        capturado_por_fk=current_user.id,
        comentario=comentario,
    )
    db.session.add(pago)
    db.session.flush()  # Asigna pago.id (lo necesitamos para armar el folio) y refleja el pago en saldo_pendiente()

    pago.folio = f'PAGO-{hoy_local().year}-{pago.id:06d}'

    cargo.actualizar_estatus()
    db.session.commit()

    enviado, error_correo = enviar_comprobante_pago(cargo.alumno, cargo, pago)

    flash(f'Pago de ${monto_pagado} registrado correctamente. Folio: {pago.folio}', 'success')
    if enviado:
        flash(f'Comprobante enviado a {cargo.alumno.correo}.', 'success')
    else:
        flash(f'El pago se guardó bien, pero no se pudo enviar el comprobante por correo ({error_correo}).', 'warning')

    return redirect(url_for('cobros.cobros', matricula=cargo.matricula_fk))


@cobros_bp.route('/pago/<int:pago_id>/anular', methods=['POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def anular_pago(pago_id):
    """
    Anula un pago mal capturado (monto equivocado, alumno equivocado,
    método incorrecto, lo que sea) -- NUNCA se borra ni se edita, se
    marca como anulado con motivo obligatorio, y deja de contar para el
    saldo del cargo. El registro sigue existiendo para siempre en el
    historial, con quién lo anuló, cuándo, y por qué. Mismo rol que
    cancelar un cargo: es una acción financiera que corrige un error, no
    una operación del día a día.
    """
    pago = db.get_or_404(Pago, pago_id)
    cargo = pago.cargo

    if pago.anulado:
        flash('Este pago ya estaba anulado.', 'warning')
        return redirect(url_for('cobros.cobros', matricula=cargo.matricula_fk))

    motivo = request.form.get('motivo_anulacion', '').strip()
    if len(motivo) < 5:
        flash('Indica un motivo (mínimo 5 caracteres) para anular este pago.', 'danger')
        return redirect(url_for('cobros.cobros', matricula=cargo.matricula_fk))

    pago.anulado = True
    pago.fecha_anulacion = ahora_utc()
    pago.anulado_por_fk = current_user.id
    pago.motivo_anulacion = motivo

    # Recalcula el estatus del cargo con este pago ya excluido -- si
    # estaba "Pagado" y este pago era el que lo completaba, regresa solo
    # a "Parcial" o "Pendiente" según lo que quede.
    cargo.actualizar_estatus()
    db.session.commit()

    flash(
        f'Pago {pago.folio or ("#" + str(pago.id))} anulado (${pago.monto_pagado}). '
        f'El saldo pendiente de este cargo se actualizó.',
        'success'
    )
    return redirect(url_for('cobros.cobros', matricula=cargo.matricula_fk))


@cobros_bp.route('/cobro/<int:cargo_id>/cancelar', methods=['POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def cancelar_cargo(cargo_id):
    cargo = db.get_or_404(Cargo, cargo_id)
    comentario = request.form.get('comentario', '').strip() or None

    if cargo.total_pagado() > 0:
        flash(
            f'No se puede cancelar: este cargo ya tiene ${cargo.total_pagado()} '
            'en pagos registrados. Cancelarlo dejaría ese dinero sin cargo al '
            'que pertenecer. Si el cargo está mal, contacta a Dirección para '
            'decidir cómo reasignar o reembolsar esos pagos antes de cancelar.',
            'danger'
        )
        return redirect(url_for('cobros.cobros', matricula=cargo.matricula_fk))

    cargo.estatus = EstatusCargo.CANCELADO
    cargo.comentario = comentario
    db.session.commit()

    flash(f'Cargo "{cargo.concepto}" cancelado.', 'success')
    return redirect(url_for('cobros.cobros', matricula=cargo.matricula_fk))


@cobros_bp.route('/cobro/<int:cargo_id>/condonar-recargo', methods=['POST'])
@rol_requerido('DIRECTIVO')
def condonar_recargo(cargo_id):
    """
    Ajuste manual del recargo -- exclusivo de Dirección. Solo puede
    REDUCIR el recargo (nunca aumentarlo por esta vía; para eso está la
    configuración automática). Una vez condonado, el cargo queda
    "congelado": actualizar_recargo_si_vencido() ya no lo vuelve a subir
    solo, para no borrar la condonación en la siguiente consulta.
    """
    cargo = db.get_or_404(Cargo, cargo_id)

    if cargo.estatus == EstatusCargo.CANCELADO:
        flash('Este cargo está cancelado; no tiene sentido condonarle recargo.', 'danger')
        return redirect(url_for('cobros.cobros', matricula=cargo.matricula_fk))

    nuevo_recargo_raw = request.form.get('nuevo_recargo', '').strip()
    motivo = request.form.get('motivo_condonacion', '').strip()

    errores = []
    if not motivo:
        errores.append('Escribe el motivo de la condonación (queda registrado en el cargo).')

    nuevo_recargo = None
    try:
        nuevo_recargo = Decimal(nuevo_recargo_raw)
        if nuevo_recargo < 0:
            errores.append('El recargo no puede quedar en negativo.')
        elif nuevo_recargo > cargo.recargo_aplicado:
            errores.append('Esta acción solo puede REDUCIR el recargo, no aumentarlo.')
    except InvalidOperation:
        errores.append('El nuevo recargo no es un número válido.')

    if errores:
        for error in errores:
            flash(error, 'danger')
        return redirect(url_for('cobros.cobros', matricula=cargo.matricula_fk))

    anterior = cargo.recargo_aplicado
    cargo.recargo_aplicado = nuevo_recargo
    cargo.recargo_congelado = True
    cargo.comentario = f'Recargo condonado por {current_user.nombre_completo}: ${anterior} -> ${nuevo_recargo}. Motivo: {motivo}'
    cargo.actualizar_estatus()
    db.session.commit()

    flash(f'Recargo de "{cargo.concepto}" ajustado de ${anterior} a ${nuevo_recargo}.', 'success')
    return redirect(url_for('cobros.cobros', matricula=cargo.matricula_fk))


@cobros_bp.route('/pago/<int:pago_id>/recibo')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')
def recibo_pago(pago_id):
    pago = db.get_or_404(Pago, pago_id)
    return render_template('recibo_pago.html', pago=pago, cargo=pago.cargo, alumno=pago.cargo.alumno)


@cobros_bp.route('/alumno/<matricula>/estado-cuenta')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')
def estado_cuenta(matricula):
    """
    "Tira de pagos" del alumno durante todo su ciclo escolar: histórico
    completo de cargos y pagos, imprimible. A diferencia de /cobros (que
    es la pantalla de trabajo diario), esta vista es de solo lectura,
    pensada para entregarse o archivarse.
    """
    alumno = db.get_or_404(Alumno, matricula)
    cargos = Cargo.query.filter_by(matricula_fk=matricula).order_by(Cargo.fecha_generacion.asc()).all()

    for cargo in cargos:
        cargo.actualizar_recargo_si_vencido()
    db.session.commit()

    total_cargado = sum((c.monto + c.recargo_aplicado for c in cargos), Decimal('0.00'))
    total_pagado = sum((c.total_pagado() for c in cargos), Decimal('0.00'))
    saldo_total = alumno.saldo_total_adeudado()

    return render_template(
        'estado_cuenta.html',
        alumno=alumno,
        cargos=cargos,
        total_cargado=total_cargado,
        total_pagado=total_pagado,
        saldo_total=saldo_total,
    )


@cobros_bp.route('/cobros/generar-mensualidades', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def generar_mensualidades():
    """
    Genera en lote el cargo de mensualidad de un periodo para TODOS los
    alumnos ACTIVOS, usando el monto_mensualidad configurado en el Plan
    de Estudios de cada quien (distinto por carrera). Reutiliza
    _cargo_duplicado() para nunca generar dos veces el mismo cargo a la
    misma persona si el botón se aprieta más de una vez por accidente.
    """
    conceptos_activos = ConceptoCobro.query.filter_by(activo=True).order_by(ConceptoCobro.nombre.asc()).all()

    if request.method == 'POST':
        concepto_cobro_id_raw = request.form.get('concepto_cobro_id', '').strip()
        periodo_escolar = request.form.get('periodo_escolar', '').strip()
        fecha_vencimiento_raw = request.form.get('fecha_vencimiento', '').strip()

        errores = []
        concepto_cobro = None
        if not concepto_cobro_id_raw:
            errores.append('Selecciona un concepto del catálogo.')
        else:
            try:
                concepto_cobro = db.session.get(ConceptoCobro, int(concepto_cobro_id_raw))
            except (ValueError, TypeError):
                concepto_cobro = None
            if not concepto_cobro or not concepto_cobro.activo:
                errores.append('El concepto seleccionado no es válido.')

        if not periodo_escolar:
            errores.append('El periodo escolar es obligatorio (ej. "Marzo 2026") para no mezclar mensualidades de distintos meses.')

        fecha_vencimiento = None
        if fecha_vencimiento_raw:
            try:
                fecha_vencimiento = datetime.strptime(fecha_vencimiento_raw, '%Y-%m-%d').date()
            except ValueError:
                errores.append('La fecha de vencimiento no es válida.')
        else:
            # Regla de la institución: el alumno tiene del 1 al 10 de cada
            # mes para pagar mensualidad/reinscripción -- si no se indica
            # fecha, se asume el día 10 (o el del mes siguiente si ya pasó).
            fecha_vencimiento = datetime.strptime(_vencimiento_dia_10_sugerido(), '%Y-%m-%d').date()

        if errores:
            for error in errores:
                flash(error, 'danger')
            return render_template('generar_mensualidades.html', conceptos=conceptos_activos, periodo_escolar_sugerido=periodo_escolar_actual(), vencimiento_sugerido=_vencimiento_dia_10_sugerido())

        alumnos_activos = Alumno.query.filter_by(estatus=EstatusAlumno.ACTIVO).order_by(Alumno.nombre_completo.asc()).all()

        generados = []
        omitidos = []
        for alumno in alumnos_activos:
            if not alumno.plan or alumno.plan.monto_mensualidad is None:
                omitidos.append({'alumno': alumno, 'motivo': 'Su plan de estudios no tiene mensualidad configurada.'})
                continue
            if _cargo_duplicado(alumno.matricula_id, concepto_cobro.id, periodo_escolar):
                omitidos.append({'alumno': alumno, 'motivo': f'Ya tiene un cargo de "{concepto_cobro.nombre}" para "{periodo_escolar}".'})
                continue

            nuevo = Cargo(
                matricula_fk=alumno.matricula_id,
                concepto_cobro_fk=concepto_cobro.id,
                concepto=concepto_cobro.nombre,
                monto=alumno.plan.monto_mensualidad,
                periodo_escolar=periodo_escolar,
                fecha_vencimiento=fecha_vencimiento,
                generado_por_fk=current_user.id,
            )
            db.session.add(nuevo)
            generados.append(alumno)

        try:
            db.session.commit()
        except IntegrityError:
            # CONCURRENCIA: todo el lote se hace en una sola transacción, así
            # que si CUALQUIER cargo del lote choca con el índice único
            # (migración b7e2c9a41f3d) -- ej. alguien ya generó ese mismo
            # cargo a mano, o el botón se apretó dos veces -- se descarta el
            # lote COMPLETO (nunca queda a medias) y se pide reintentar; al
            # reintentar, _cargo_duplicado() ya va a omitir solo los que de
            # verdad ya existan.
            db.session.rollback()
            flash(
                'No se generó el lote: alguno de estos cargos ya se generó justo '
                'ahora por otra operación (ej. el botón se apretó dos veces). '
                'Vuelve a intentarlo -- los que ya existan se omitirán solos.',
                'danger'
            )
            return render_template(
                'generar_mensualidades.html',
                conceptos=conceptos_activos,
                periodo_escolar_sugerido=periodo_escolar_actual(),
                vencimiento_sugerido=_vencimiento_dia_10_sugerido(),
            )

        flash(
            f'Se generaron {len(generados)} cargo(s) de "{concepto_cobro.nombre}" - {periodo_escolar}. '
            f'{len(omitidos)} alumno(s) se omitieron (ver detalle abajo).',
            'success' if generados else 'warning'
        )
        return render_template(
            'generar_mensualidades.html',
            conceptos=conceptos_activos,
            generados=generados,
            omitidos=omitidos,
            periodo_escolar_sugerido=periodo_escolar_actual(),
            vencimiento_sugerido=_vencimiento_dia_10_sugerido(),
        )

    return render_template('generar_mensualidades.html', conceptos=conceptos_activos, periodo_escolar_sugerido=periodo_escolar_actual(), vencimiento_sugerido=_vencimiento_dia_10_sugerido())


@cobros_bp.route('/cobros/recordatorios-vencimiento', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def recordatorios_vencimiento():
    """
    Envío manual (disparado por un clic, no por cron todavía) de un correo
    recordatorio a los alumnos con un cargo que vence en los próximos
    DIAS_AVISO_VENCIMIENTO días. No lleva registro de "ya se avisó" -- si
    se aprieta el botón varias veces el mismo día, se reenvía. Pensado
    para correrse una vez al día; cuando el sistema esté en el VPS se
    puede automatizar con un cron que llame esta misma ruta.
    """
    hoy = hoy_local()
    limite = hoy + timedelta(days=DIAS_AVISO_VENCIMIENTO)

    candidatos = (
        Cargo.query
        .filter(Cargo.estatus.in_([EstatusCargo.PENDIENTE, EstatusCargo.PARCIAL]))
        .filter(Cargo.fecha_vencimiento.isnot(None))
        .filter(Cargo.fecha_vencimiento >= hoy, Cargo.fecha_vencimiento <= limite)
        .all()
    )

    if request.method == 'POST':
        enviados = []
        fallidos = []
        for cargo in candidatos:
            ok, error = enviar_recordatorio_vencimiento(cargo.alumno, cargo)
            if ok:
                enviados.append(cargo)
            else:
                fallidos.append({'cargo': cargo, 'motivo': error})

        flash(f'{len(enviados)} recordatorio(s) enviado(s). {len(fallidos)} no se pudieron mandar.', 'success' if enviados else 'warning')
        return render_template('recordatorios_vencimiento.html', candidatos=candidatos, enviados=enviados, fallidos=fallidos, dias_aviso=DIAS_AVISO_VENCIMIENTO)

    return render_template('recordatorios_vencimiento.html', candidatos=candidatos, dias_aviso=DIAS_AVISO_VENCIMIENTO)
