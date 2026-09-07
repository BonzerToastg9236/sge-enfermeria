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
    ('/', ('GET',), 'index', None),
    ('/alumno/<matricula>/avanzar-cuatrimestre', ('POST',), 'avanzar_cuatrimestre', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/alumno/<matricula>/becas', ('GET', 'POST'), 'becas_alumno', ('DIRECTIVO', 'CONTADOR')),
    ('/alumno/<matricula>/boleta', ('GET', 'POST'), 'boleta', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/alumno/<matricula>/cambiar-estatus', ('POST',), 'cambiar_estatus', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/alumno/<matricula>/carga-academica', ('GET',), 'carga_academica', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/alumno/<matricula>/cobros', ('GET',), 'cobros', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
    ('/alumno/<matricula>/cobros/nuevo', ('POST',), 'nuevo_cargo', ('DIRECTIVO', 'CONTADOR')),
    ('/alumno/<matricula>/documento/<int:doc_id>/ver', ('GET',), 'ver_documento', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/alumno/<matricula>/documentos', ('GET', 'POST'), 'documentos', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/alumno/<matricula>/estado-cuenta', ('GET',), 'estado_cuenta', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
    ('/alumno/<matricula>/expediente', ('GET',), 'ver_expediente', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/alumno/<matricula>/ficha', ('GET',), 'ficha_inscripcion', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/alumno/<matricula>/historial-calificaciones', ('GET',), 'historial_calificaciones', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/alumnos/avanzar-cuatrimestre-lote', ('GET', 'POST'), 'avanzar_cuatrimestre_lote', ('DIRECTIVO', 'ADMINISTRATIVO')),
    ('/alumnos/importar', ('GET', 'POST'), 'importar_alumnos', ('DIRECTIVO',)),
    ('/alumnos/importar/plantilla', ('GET',), 'plantilla_importacion', ('DIRECTIVO',)),
    ('/becas/<int:beca_id>/desactivar', ('POST',), 'desactivar_beca', ('DIRECTIVO', 'CONTADOR')),
    ('/boletas/importar', ('GET',), 'boletas_importar', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/boletas/importar', ('POST',), 'importar_boletas', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/boletas/importar/plantilla', ('GET',), 'plantilla_boletas', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/buscar', ('POST',), 'buscar', None),
    ('/cobro/<int:cargo_id>/cancelar', ('POST',), 'cancelar_cargo', ('DIRECTIVO', 'CONTADOR')),
    ('/cobro/<int:cargo_id>/condonar-recargo', ('POST',), 'condonar_recargo', ('DIRECTIVO',)),
    ('/cobro/<int:cargo_id>/pagar', ('POST',), 'registrar_pago', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
    ('/cobros/dashboard', ('GET',), 'dashboard_cobros', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
    ('/cobros/dashboard/exportar', ('GET',), 'exportar_dashboard_cobros', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
    ('/cobros/generar-mensualidades', ('GET', 'POST'), 'generar_mensualidades', ('DIRECTIVO', 'CONTADOR')),
    ('/cobros/recordatorios-vencimiento', ('GET', 'POST'), 'recordatorios_vencimiento', ('DIRECTIVO', 'CONTADOR')),
    ('/conceptos-cobro', ('GET', 'POST'), 'conceptos_cobro', ('DIRECTIVO', 'CONTADOR')),
    ('/conceptos-cobro/<int:concepto_id>/editar-precio', ('POST',), 'editar_precio_concepto', ('DIRECTIVO', 'CONTADOR')),
    ('/conceptos-cobro/<int:concepto_id>/toggle', ('POST',), 'toggle_concepto_cobro', ('DIRECTIVO', 'CONTADOR')),
    ('/configuracion/cobros', ('GET', 'POST'), 'configuracion_cobros', ('DIRECTIVO', 'CONTADOR')),
    ('/configuracion/institucion', ('GET', 'POST'), 'configuracion_institucion', ('DIRECTIVO',)),
    ('/documento/<int:doc_id>/eliminar', ('POST',), 'eliminar_documento', ('DIRECTIVO',)),
    ('/login', ('GET', 'POST'), 'login', None),
    ('/logout', ('GET',), 'logout', None),
    ('/materias/<int:materia_id>/eliminar', ('POST',), 'eliminar_materia', ('DIRECTIVO',)),
    ('/pago/<int:pago_id>/anular', ('POST',), 'anular_pago', ('DIRECTIVO', 'CONTADOR')),
    ('/pago/<int:pago_id>/recibo', ('GET',), 'recibo_pago', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
    ('/perfil', ('GET', 'POST'), 'perfil', None),
    ('/planes/<int:plan_id>/materias', ('GET', 'POST'), 'gestionar_materias', ('DIRECTIVO',)),
    ('/planes/mensualidades', ('GET', 'POST'), 'planes_mensualidades', ('DIRECTIVO',)),
    ('/registro', ('GET', 'POST'), 'registro', None),
    ('/reportes/cartera-vencida', ('GET',), 'cartera_vencida', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
    ('/reportes/cartera-vencida/exportar', ('GET',), 'exportar_cartera_vencida', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
    ('/reportes/cobros-del-dia', ('GET',), 'reporte_cobros_del_dia', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
    ('/reportes/cobros-del-dia/exportar', ('GET',), 'exportar_reporte_cobros_del_dia', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
    ('/usuarios', ('GET',), 'usuarios', ('DIRECTIVO',)),
    ('/usuarios/<int:user_id>/toggle', ('POST',), 'toggle_usuario', ('DIRECTIVO',)),
    ('/usuarios/nuevo', ('GET', 'POST'), 'nuevo_usuario', ('DIRECTIVO',)),
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
