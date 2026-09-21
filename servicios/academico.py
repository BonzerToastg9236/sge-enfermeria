"""
Avance de periodo (cuatrimestre), carga académica y helpers del módulo de
boletas/calificaciones.
"""

from flask_login import current_user

from extensiones import db
from modelos import (
    ConfiguracionInstitucion, EstatusAlumno, InscripcionMateria, Materia,
    HistorialCalificacion,
)
from servicios.cobros import _generar_cargos_de_reinscripcion
from servicios.terminologia import terminos
from utilidades.fechas import periodo_escolar_actual
from utilidades.folios import siguiente_folio


def _max_periodos() -> int:
    """
    Tope de periodos (cuatrimestres/grados/semestres/lo que sea) según
    la configuración de la institución -- reemplaza al antiguo
    _max_periodos(), que era un número fijo en
    config.py y no se podía ajustar por tipo de escuela sin editar código.
    """
    return ConfiguracionInstitucion.obtener().max_periodos


def _generar_carga_academica(alumno):
    """
    Registra la "carga académica" del alumno: una fila por cada materia
    de SU plan y SU cuatrimestre actual (Escudo del Plan de Estudios
    aplicado también aquí -- nunca se inscribe una materia de otra
    carrera). Se corre junto con _generar_cargos_de_inscripcion() al
    activarlo por primera vez.
    """
    periodo_actual = periodo_escolar_actual()
    materias_del_cuatrimestre = (
        Materia.query
        .filter_by(id_plan_fk=alumno.id_plan_fk, cuatrimestre=alumno.cuatrimestre_actual, activa=True)
        .order_by(Materia.nombre.asc())
        .all()
    )

    inscripciones_generadas = []
    for materia in materias_del_cuatrimestre:
        ya_existe = InscripcionMateria.query.filter_by(
            matricula_fk=alumno.matricula_id,
            id_materia_fk=materia.id,
            periodo_escolar=periodo_actual,
        ).first()
        if ya_existe:
            continue
        inscripcion = InscripcionMateria(
            matricula_fk=alumno.matricula_id,
            id_materia_fk=materia.id,
            periodo_escolar=periodo_actual,
        )
        db.session.add(inscripcion)
        inscripciones_generadas.append(inscripcion)

    return inscripciones_generadas


def _avanzar_cuatrimestre(alumno):
    """
    Avanza al alumno al SIGUIENTE cuatrimestre: incrementa
    cuatrimestre_actual, genera su cargo de Reinscripción + las
    mensualidades del nuevo cuatrimestre (mismo criterio que al activarlo
    por primera vez), y su nueva carga académica -- reutilizando los
    mismos helpers, así que nunca duplica nada.

    Devuelve (ok, mensaje, cargos_generados, materias_generadas,
    avisos_de_configuracion) -- lo último es lo que haga falta capturar en
    la configuración de precios de la institución (ver
    _generar_cargos_de_periodo()).
    No avanza (ok=False) si el alumno no está Activo, o si ya está en el
    último cuatrimestre configurado.
    """
    max_cuatri = _max_periodos()

    if alumno.estatus != EstatusAlumno.ACTIVO:
        return False, f'{alumno.nombre_completo} no está Activo (está en "{alumno.estatus.value}"), no se puede avanzar.', [], [], []

    if alumno.cuatrimestre_actual >= max_cuatri:
        return False, f'{alumno.nombre_completo} ya está en {max_cuatri}° {terminos().periodo_l}, el máximo configurado.', [], [], []

    alumno.cuatrimestre_actual += 1

    cargos_generados, avisos_de_configuracion = _generar_cargos_de_reinscripcion(alumno)

    # _generar_carga_academica() lee alumno.cuatrimestre_actual, que ya
    # quedó incrementado arriba -- por eso genera la del cuatrimestre NUEVO.
    materias_generadas = _generar_carga_academica(alumno)

    mensaje = f'{alumno.nombre_completo} avanzó a {alumno.cuatrimestre_actual}° {terminos().periodo_l}.'
    return True, mensaje, cargos_generados, materias_generadas, avisos_de_configuracion


def _registrar_historial_calificacion(alumno, materia, periodo_escolar, calificacion_anterior, calificacion_nueva):
    """Deja rastro de CADA captura o corrección de calificación -- nunca se sobreescribe ni se borra."""
    db.session.add(HistorialCalificacion(
        matricula_fk=alumno.matricula_id,
        id_materia_fk=materia.id,
        periodo_escolar=periodo_escolar,
        usuario_fk=current_user.id,
        calificacion_anterior=calificacion_anterior,
        calificacion_nueva=calificacion_nueva,
    ))


def _siguiente_numero_acta() -> str:
    """
    Genera un folio nuevo de acta, estilo "ACTA-2026-000042" (mismo patrón
    que Pago.folio). Ya NO es un campo que capture el usuario -- se genera
    uno por cada envío del formulario de boleta o de carga masiva, y se
    comparte entre todas las materias guardadas en ese mismo envío.

    ACTUALIZACIÓN: antes esto buscaba el máximo numero_acta existente y le
    sumaba 1 -- funcionaba mientras solo una persona capturara boletas a
    la vez, pero tenía una condición de carrera real (dos personas
    guardando boletas en el mismo instante podían recibir el mismo
    folio). Ahora usa siguiente_folio(), que sí protege contra eso -- ver
    ContadorFolio y siguiente_folio() para el detalle de la protección.
    """
    return siguiente_folio(tipo='ACTA', prefijo='ACTA', digitos=6)
