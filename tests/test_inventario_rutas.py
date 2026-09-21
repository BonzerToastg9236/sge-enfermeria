"""
Segunda parte de la red de seguridad de Plan 2 (ver
test_integridad_urls.py). Fija el inventario completo de las 51 rutas de
hoy: URL, métodos HTTP y roles exigidos por rol_requerido(). Si un paso de
la migración cambia por accidente la URL pública, agrega o quita un
método, o afloja/endurece el control de acceso de una ruta, esta prueba
lo detecta.

Esta tabla se generó leyendo app.url_map en vivo (no a mano), así que es
un reflejo exacto del comportamiento real, no de lo que alguien recuerda
que debería ser.

MANTENIMIENTO DURANTE PLAN 2: cuando una ruta se mueve a un blueprint, su
endpoint cambia de 'x' a 'blueprint.x' (Flask lo hace automático). Cada
tarea que mueve rutas actualiza en este archivo -ÚNICAMENTE- el campo de
endpoint de las filas que movió, nunca la URL, los métodos ni los roles.
Eso es lo que la prueba está diseñada para permitir: rastrear el
renombrado intencional, no ocultar un cambio de comportamiento.
"""

from app import app


def _roles_de_vista(func):
    """
    rol_requerido() envuelve a la vista con @wraps + @login_required, así
    que la tupla de roles no vive en el primer nivel de closure -- hay que
    recorrer los closures anidados hasta encontrarla. Devuelve None si la
    vista no usa rol_requerido (pública, o solo @login_required).
    """
    visitados = set()
    pendientes = [func]
    while pendientes:
        actual = pendientes.pop()
        if id(actual) in visitados or not hasattr(actual, '__closure__'):
            continue
        visitados.add(id(actual))
        cierre = actual.__closure__
        if not cierre:
            continue
        for nombre, celda in zip(actual.__code__.co_freevars, cierre):
            try:
                valor = celda.cell_contents
            except ValueError:
                continue
            if nombre == 'roles_permitidos' and isinstance(valor, tuple):
                return valor
            if callable(valor):
                pendientes.append(valor)
    return None


# (URL, métodos, endpoint, roles) -- generado desde app.url_map el
# 2026-09-07, antes de mover cualquier ruta. `None` en roles significa
# "sin rol_requerido" (público o solo @login_required).
INVENTARIO_ESPERADO = [
    ('/', ('GET',), 'alumnos.index', None),
    ('/alumno/<matricula>/avanzar-cuatrimestre', ('POST',), 'alumnos.avanzar_cuatrimestre', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/alumno/<matricula>/becas', ('GET', 'POST'), 'cobros.becas_alumno', ('DIRECTIVO', 'CONTADOR')),
    ('/alumno/<matricula>/boleta', ('GET', 'POST'), 'academico.boleta', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/alumno/<matricula>/cambiar-estatus', ('POST',), 'alumnos.cambiar_estatus', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/alumno/<matricula>/carga-academica', ('GET',), 'academico.carga_academica', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/alumno/<matricula>/cobros', ('GET',), 'cobros.cobros', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
    ('/alumno/<matricula>/cobros/nuevo', ('POST',), 'cobros.nuevo_cargo', ('DIRECTIVO', 'CONTADOR')),
    ('/alumno/<matricula>/documento/<int:doc_id>/ver', ('GET',), 'documentos.ver_documento', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/alumno/<matricula>/documentos', ('GET', 'POST'), 'documentos.documentos', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/alumno/<matricula>/estado-cuenta', ('GET',), 'cobros.estado_cuenta', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
    ('/alumno/<matricula>/expediente', ('GET',), 'alumnos.ver_expediente', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/alumno/<matricula>/ficha', ('GET',), 'alumnos.ficha_inscripcion', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/alumno/<matricula>/historial-calificaciones', ('GET',), 'academico.historial_calificaciones', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/alumnos/avanzar-cuatrimestre-lote', ('GET', 'POST'), 'alumnos.avanzar_cuatrimestre_lote', ('DIRECTIVO', 'ADMINISTRATIVO')),
    ('/alumnos/importar', ('GET', 'POST'), 'alumnos.importar_alumnos', ('DIRECTIVO',)),
    ('/alumnos/importar/plantilla', ('GET',), 'alumnos.plantilla_importacion', ('DIRECTIVO',)),
    ('/becas/<int:beca_id>/desactivar', ('POST',), 'cobros.desactivar_beca', ('DIRECTIVO', 'CONTADOR')),
    ('/boletas/importar', ('GET',), 'academico.boletas_importar', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/boletas/importar', ('POST',), 'academico.importar_boletas', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/boletas/importar/plantilla', ('GET',), 'academico.plantilla_boletas', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/auditoria', ('GET',), 'auditoria.bitacora', ('DIRECTIVO',)),
    ('/buscar', ('POST',), 'alumnos.buscar', None),
    ('/cobro/<int:cargo_id>/cancelar', ('POST',), 'cobros.cancelar_cargo', ('DIRECTIVO', 'CONTADOR')),
    ('/cobro/<int:cargo_id>/condonar-recargo', ('POST',), 'cobros.condonar_recargo', ('DIRECTIVO',)),
    ('/cobro/<int:cargo_id>/pagar', ('POST',), 'cobros.registrar_pago', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
    ('/cobros/dashboard', ('GET',), 'reportes.dashboard_cobros', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
    ('/cobros/dashboard/exportar', ('GET',), 'reportes.exportar_dashboard_cobros', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
    ('/cobros/generar-mensualidades', ('GET', 'POST'), 'cobros.generar_mensualidades', ('DIRECTIVO', 'CONTADOR')),
    ('/cobros/recordatorios-vencimiento', ('GET', 'POST'), 'cobros.recordatorios_vencimiento', ('DIRECTIVO', 'CONTADOR')),
    ('/conceptos-cobro', ('GET', 'POST'), 'configuracion.conceptos_cobro', ('DIRECTIVO', 'CONTADOR')),
    ('/conceptos-cobro/<int:concepto_id>/editar-precio', ('POST',), 'configuracion.editar_precio_concepto', ('DIRECTIVO', 'CONTADOR')),
    ('/conceptos-cobro/<int:concepto_id>/toggle', ('POST',), 'configuracion.toggle_concepto_cobro', ('DIRECTIVO', 'CONTADOR')),
    ('/configuracion/cobros', ('GET', 'POST'), 'configuracion.configuracion_cobros', ('DIRECTIVO', 'CONTADOR')),
    ('/configuracion/cobros/simular', ('GET',), 'configuracion.simular_recargo', ('DIRECTIVO', 'CONTADOR')),
    ('/configuracion/institucion', ('GET', 'POST'), 'configuracion.configuracion_institucion', ('DIRECTIVO',)),
    ('/documento/<int:doc_id>/eliminar', ('POST',), 'documentos.eliminar_documento', ('DIRECTIVO',)),
    ('/login', ('GET', 'POST'), 'auth.login', None),
    ('/logout', ('GET',), 'auth.logout', None),
    ('/materias/<int:materia_id>/eliminar', ('POST',), 'configuracion.eliminar_materia', ('DIRECTIVO',)),
    ('/pago/<int:pago_id>/anular', ('POST',), 'cobros.anular_pago', ('DIRECTIVO', 'CONTADOR')),
    ('/pago/<int:pago_id>/recibo', ('GET',), 'cobros.recibo_pago', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
    ('/perfil', ('GET', 'POST'), 'auth.perfil', None),
    ('/planes/<int:plan_id>/materias', ('GET', 'POST'), 'configuracion.gestionar_materias', ('DIRECTIVO',)),
    ('/planes/<int:plan_id>/editar', ('POST',), 'configuracion.editar_plan', ('DIRECTIVO',)),
    ('/planes/<int:plan_id>/eliminar', ('POST',), 'configuracion.eliminar_plan', ('DIRECTIVO',)),
    ('/planes/<int:plan_id>/toggle', ('POST',), 'configuracion.toggle_plan', ('DIRECTIVO',)),
    ('/planes/nuevo', ('POST',), 'configuracion.nuevo_plan', ('DIRECTIVO',)),
    ('/materias/<int:materia_id>/editar', ('POST',), 'configuracion.editar_materia', ('DIRECTIVO',)),
    ('/materias/<int:materia_id>/toggle', ('POST',), 'configuracion.toggle_materia', ('DIRECTIVO',)),
    ('/conceptos-cobro/<int:concepto_id>/eliminar', ('POST',), 'configuracion.eliminar_concepto', ('DIRECTIVO', 'CONTADOR')),
    ('/conceptos-cobro/<int:concepto_id>/renombrar', ('POST',), 'configuracion.renombrar_concepto', ('DIRECTIVO', 'CONTADOR')),
    ('/planes/mensualidades', ('GET', 'POST'), 'configuracion.planes_mensualidades', ('DIRECTIVO',)),
    ('/registro', ('GET', 'POST'), 'registro.registro', None),
    ('/reportes/cartera-vencida', ('GET',), 'reportes.cartera_vencida', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
    ('/reportes/cartera-vencida/exportar', ('GET',), 'reportes.exportar_cartera_vencida', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
    ('/reportes/cobros-del-dia', ('GET',), 'reportes.reporte_cobros_del_dia', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
    ('/reportes/cobros-del-dia/exportar', ('GET',), 'reportes.exportar_reporte_cobros_del_dia', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
    ('/usuarios', ('GET',), 'usuarios.usuarios', ('DIRECTIVO',)),
    ('/usuarios/<int:user_id>/toggle', ('POST',), 'usuarios.toggle_usuario', ('DIRECTIVO',)),
    ('/usuarios/nuevo', ('GET', 'POST'), 'usuarios.nuevo_usuario', ('DIRECTIVO',)),
]


def test_el_numero_de_rutas_no_cambia(app):
    reglas = [r for r in app.url_map.iter_rules() if r.endpoint != 'static']
    assert len(reglas) == len(INVENTARIO_ESPERADO), (
        f'Se esperaban {len(INVENTARIO_ESPERADO)} rutas, hay {len(reglas)}. '
        'Si agregaste o quitaste una ruta a propósito, actualiza '
        'INVENTARIO_ESPERADO explícitamente -- esta prueba no debe '
        'ajustarse en silencio.'
    )


def test_cada_ruta_conserva_url_metodos_y_control_de_acceso(app):
    reglas_por_endpoint = {
        regla.endpoint: regla
        for regla in app.url_map.iter_rules()
        if regla.endpoint != 'static'
    }

    fallas = []
    for url_esperada, metodos_esperados, endpoint, roles_esperados in INVENTARIO_ESPERADO:
        regla = reglas_por_endpoint.get(endpoint)
        if regla is None:
            fallas.append(f'Endpoint "{endpoint}" ya no existe (¿se renombró sin actualizar esta tabla?)')
            continue

        if str(regla) != url_esperada:
            fallas.append(f'{endpoint}: URL cambió de "{url_esperada}" a "{regla}"')

        metodos_reales = tuple(sorted(regla.methods - {'HEAD', 'OPTIONS'}))
        if metodos_reales != metodos_esperados:
            fallas.append(f'{endpoint}: métodos cambiaron de {metodos_esperados} a {metodos_reales}')

        roles_reales = _roles_de_vista(app.view_functions[endpoint])
        if roles_reales != roles_esperados:
            fallas.append(f'{endpoint}: roles cambiaron de {roles_esperados} a {roles_reales}')

    assert not fallas, 'Cambios no esperados en el control de rutas:\n' + '\n'.join(fallas)
