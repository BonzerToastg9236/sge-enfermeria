"""
Sistema de Gestión Escolar (SGE) - Universidad de Enfermería
--------------------------------------------------------------
Paso 1: Configuración inicial de Flask + Modelos de Base de Datos (SQLAlchemy)

Reglas de negocio implementadas a nivel de modelo:
  1. Los docentes NO tienen tabla/rol de acceso: no existe modelo "Maestro".
  2. "Escudo" del Plan de Estudios: Materia siempre pertenece a un PlanEstudio,
     y Alumno siempre pertenece a un PlanEstudio. La validación de que una
     calificación solo pueda capturarse si la Materia pertenece al Plan del
     Alumno se hace a nivel de lógica de aplicación (Paso 4 - Módulo de
     Captura de Calificaciones), pero el modelo ya deja la estructura lista
     mediante las relaciones y un método de validación auxiliar.
  3. Centrado en el Alumno: Calificacion se vincula a matricula (Alumno),
     nunca a un "Grupo". El alumno puede cambiar de grupo/generación sin
     perder su historial porque el historial cuelga de su matrícula.
"""

import enum
import io
import logging
import os
import re
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone, timedelta, date, time
from zoneinfo import ZoneInfo


from decimal import Decimal, InvalidOperation

import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from urllib.parse import urlparse

from flask import (
    Flask, render_template, request, flash, redirect, url_for, abort,
    send_file, current_app
)
from flask_login import (
    UserMixin, login_required, current_user
)
from sqlalchemy import UniqueConstraint, CheckConstraint, Index, text, or_
from sqlalchemy.exc import IntegrityError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash

from config import config_by_name
from extensiones import db, migrate, login_manager, csrf, limiter, mail
from utilidades.fechas import (
    ZONA_HORARIA_DEFAULT, ahora_utc, hoy_local, a_local,
    rango_utc_del_dia, periodo_escolar_actual,
    _filtro_fechahora, _filtro_fecha, _filtro_hora, _filtro_fecha_larga,
)
from utilidades.seguridad import load_user, rol_requerido
from utilidades.folios import siguiente_folio
from utilidades.paginacion import _paginar_lista, ALUMNOS_POR_PAGINA, CARGOS_POR_PAGINA
from modelos import (
    RolUsuario, Usuario,
    EstatusAlumno, TurnoAlumno, ModalidadEstudio,
    PlanEstudio, Materia, Alumno,
    TipoDocumento, DocumentoAlumno,
    Calificacion, HistorialEstatus, HistorialCalificacion, InscripcionMateria,
    EstatusCargo, MetodoPago, TipoRecargo, TipoDescuentoBeca,
    ConceptoCobro, Beca, ConfiguracionCobros, ConfiguracionInstitucion,
    Cargo, Pago, ContadorFolio,
)
from servicios.matriculas import generar_matricula, crear_alumno_generando_matricula
from servicios.alumnos import _matriculas_con_adeudo, calcular_estadisticas_alumnos
from servicios.cobros import (
    _cargo_duplicado, _meses_del_cuatrimestre_actual, _monto_mensualidad_con_beca,
    _generar_cargos_de_periodo, _generar_cargos_de_inscripcion,
    _generar_cargos_de_reinscripcion, MESES_ES, _vencimiento_dia_10_sugerido,
)
from servicios.academico import (
    _max_periodos, _generar_carga_academica, _avanzar_cuatrimestre,
)
from servicios.reportes import (
    _calcular_reporte_cobros_del_dia, _calcular_cartera_vencida,
    _rango_ultimos_n_meses, _calcular_dashboard_cobros,
)
from servicios.correo import (
    MOTIVO_GENERICO_FALLO_CORREO, enviar_comprobante_pago,
    DIAS_AVISO_VENCIMIENTO, enviar_recordatorio_vencimiento,
)


def siguiente_folio(tipo: str, prefijo: str, digitos: int = 6, intentos_maximos: int = 5) -> str:
    """
    Genera un folio consecutivo único y seguro ante concurrencia, con
    formato "{prefijo}-{año}-{consecutivo con relleno de ceros}" (ej.
    "ACTA-2026-000042"). Reutilizable para cualquier folio futuro que,
    como numero_acta, necesite compartirse entre varias filas de otra
    tabla y por lo tanto no pueda protegerse con una UniqueConstraint
    directa sobre esa tabla.

    IMPORTANTE PARA QUIEN LLAME A ESTA FUNCIÓN: debe invocarse ANTES de
    agregar a la sesión cualquier otro objeto pendiente de esta misma
    petición (igual que ya hacían boleta() e importar_boletas() con
    _siguiente_numero_acta()). Si ocurre una colisión real en el primer
    folio del año (caso límite, ver docstring de ContadorFolio) esta
    función hace rollback() de la sesión para reintentar limpio -- si ya
    hubiera otros objetos sin commitear en la sesión en ese momento, ese
    rollback también los descartaría.
    """
    anio_actual = hoy_local().year

    for _intento in range(intentos_maximos):
        try:
            consulta = ContadorFolio.query.filter_by(tipo=tipo, anio=anio_actual)
            # Mismo criterio que generar_matricula(): with_for_update() solo
            # tiene efecto real en motores que soportan bloqueo por fila.
            if db.engine.dialect.name != 'sqlite':
                consulta = consulta.with_for_update()
            contador = consulta.first()

            if contador is None:
                contador = ContadorFolio(tipo=tipo, anio=anio_actual, ultimo_valor=0)
                db.session.add(contador)
                db.session.flush()  # Puede lanzar IntegrityError si otra transacción ya insertó este (tipo, año)

            contador.ultimo_valor += 1
            db.session.flush()
            return f'{prefijo}-{anio_actual}-{contador.ultimo_valor:0{digitos}d}'

        except IntegrityError:
            db.session.rollback()
            continue  # Probable colisión al crear la primera fila del contador: se reintenta

    raise RuntimeError(
        f'No se pudo generar un folio único de tipo "{tipo}" tras {intentos_maximos} intentos. '
        'Esto no debería ocurrir en operación normal -- revisar si hay un problema de fondo '
        'con la base de datos antes de reintentar a mano.'
    )


# ---------------------------------------------------------------------------
# FILTROS DE PLANTILLA -- fechas/horas en hora local (México)
# ---------------------------------------------------------------------------
# Las columnas DateTime se guardan en UTC (ver ahora_utc()); estos filtros
# son la única vía para mostrarlas -- las plantillas NUNCA deben llamar
# .strftime() directamente sobre un DateTime. Las columnas Date puras
# (fecha_nacimiento, fecha_vencimiento, etc.) siguen mostrándose igual:
# ya son locales por diseño y no tienen nada que convertir.


# ---------------------------------------------------------------------------
# APPLICATION FACTORY
# ---------------------------------------------------------------------------

def create_app(config_name='development'):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_by_name[config_name])

    # SECURITY-NOTE: aquí SÍ se ejecuta esta validación (a diferencia de un
    # __init__ en config.py, que Flask jamás llamaría porque from_object()
    # recibe la CLASE, no una instancia). Si en producción falta SECRET_KEY,
    # preferimos que la app truene al arrancar a que arranque en silencio
    # con una clave insegura y públicamente conocida.
    if config_name == 'production' and not app.config.get('SECRET_KEY'):
        raise RuntimeError(
            'SECRET_KEY no está definida en el entorno de producción. '
            'Genera una clave larga y aleatoria (ej. con `python -c '
            '"import secrets; print(secrets.token_hex(32))"`) y agrégala '
            'a tu archivo .env antes de arrancar la aplicación.'
        )

    if config_name == 'production':
        # El VPS sirve la app detrás de Nginx (proxy inverso). Sin esto,
        # Flask-Limiter (get_remote_address) vería SIEMPRE la IP de Nginx
        # en vez de la IP real del navegador, y el límite de intentos de
        # login se compartiría entre TODOS los usuarios en vez de aplicarse
        # por IP real. x_for=1 confía en un solo salto de proxy (ajusta si
        # tu VPS tiene más de un proxy intermedio, ej. Cloudflare + Nginx).
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    migrate.init_app(app, db, render_as_batch=True)

    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'Debes iniciar sesión para acceder a esta página.'
    login_manager.login_message_category = 'warning'

    csrf.init_app(app)
    limiter.init_app(app)
    mail.init_app(app)

    # Filtros de fecha/hora en hora local (ver sección FILTROS DE PLANTILLA
    # arriba). Sin esto, las plantillas que ya los usan (|fechahora,
    # |fecha_larga, etc.) truenan con TemplateAssertionError.
    app.add_template_filter(_filtro_fechahora, 'fechahora')
    app.add_template_filter(_filtro_fecha, 'fecha')
    app.add_template_filter(_filtro_hora, 'hora')
    app.add_template_filter(_filtro_fecha_larga, 'fecha_larga')

    # Asegura que exista la carpeta física donde se guardan los documentos
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

    # -----------------------------------------------------------------
    # LOGGING (H5)
    # -----------------------------------------------------------------
    # Antes de esto, un error en producción solo aparecía como un 500
    # genérico en el navegador del usuario, sin ningún rastro guardado en
    # el servidor -- imposible saber qué pasó después del hecho. Gunicorn
    # ya escribe su propio log de acceso/errores (ver deploy/sge.service),
    # pero eso NO captura excepciones de la lógica de la app con
    # traceback completo, que es lo que realmente hace falta para
    # diagnosticar un error reportado por Control Escolar.
    if not app.testing:
        if config_name == 'production':
            # Carpeta ya creada manualmente en el paso 7 de DEPLOYMENT.md
            # (mkdir -p ~/sge_enfermeria/logs). RotatingFileHandler evita
            # que el log crezca sin límite y llene el disco del VPS con
            # el tiempo (relacionado con D9 de la auditoría): guarda hasta
            # 5 archivos de 2 MB cada uno (10 MB totales), rotando el más
            # viejo cuando se llena.
            log_dir = os.path.join(app.instance_path, '..', 'logs')
            log_dir = os.path.abspath(log_dir)
            os.makedirs(log_dir, exist_ok=True)
            handler = RotatingFileHandler(
                os.path.join(log_dir, 'sge.log'),
                maxBytes=2 * 1024 * 1024,
                backupCount=5,
                encoding='utf-8',
            )
            handler.setLevel(logging.INFO)
        else:
            # Desarrollo: a consola, nada de archivos que limpiar a mano.
            handler = logging.StreamHandler()
            handler.setLevel(logging.DEBUG)

        handler.setFormatter(logging.Formatter(
            '[%(asctime)s] %(levelname)s in %(module)s: %(message)s'
        ))
        app.logger.handlers.clear()   # quita el handler por defecto de Flask
        app.logger.addHandler(handler)
        app.logger.propagate = False  # evita que el registro suba también al logger raíz
        app.logger.setLevel(logging.INFO if config_name == 'production' else logging.DEBUG)
        app.logger.info('SGE arrancado (config=%s)', config_name)

    from rutas.auth import auth_bp
    app.register_blueprint(auth_bp)

    from rutas.usuarios import usuarios_bp
    app.register_blueprint(usuarios_bp)

    from rutas.registro import registro_bp
    app.register_blueprint(registro_bp)

    from rutas.documentos import documentos_bp
    app.register_blueprint(documentos_bp)

    from rutas.academico import academico_bp
    app.register_blueprint(academico_bp)

    return app


app = create_app(os.environ.get('FLASK_ENV', 'development'))


@app.errorhandler(429)
def limite_intentos_excedido(error):
    """
    Se dispara cuando Flask-Limiter bloquea una IP por exceder el límite de
    intentos de login o de registro público. En vez del 429 genérico de
    Werkzeug, mostramos un mensaje claro y regresamos a la pantalla que
    corresponde.

    El caso de /registro se distingue a propósito: quien se registra es un
    aspirante SIN cuenta -- mandarlo a /login con el mensaje de "intentos
    de inicio de sesión" sería desconcertante y no le diría qué hacer.
    """
    if request.endpoint == 'registro.registro':
        flash(
            'Se enviaron demasiadas solicitudes de registro seguidas desde esta '
            'conexión. Por seguridad, espera unos minutos e inténtalo de nuevo.',
            'danger'
        )
        return redirect(url_for('registro.registro'))

    flash('Demasiados intentos de inicio de sesión. Por seguridad, espera un minuto e inténtalo de nuevo.', 'danger')
    return redirect(url_for('auth.login'))


@app.errorhandler(413)
def envio_demasiado_grande(error):
    """
    Werkzeug corta la petición cuando excede MAX_CONTENT_LENGTH, ANTES de
    que la vista corra. Sin este handler, quien sube un documento pesado
    ve la página cruda de error 413 y pierde lo que había capturado en el
    formulario, sin entender por qué.

    El destino se arma con la RUTA del referer (nunca el referer completo):
    así es imposible que alguien lo use para un redirect a un sitio externo.
    """
    limite_mb = app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)
    flash(
        f'El envío es demasiado grande (máximo {limite_mb} MB en total por envío, '
        f'sumando todos los archivos). Sube los documentos de uno en uno, o '
        f'escanéalos con menor resolución.',
        'danger'
    )

    ruta_previa = urlparse(request.referrer or '').path
    return redirect(ruta_previa if ruta_previa.startswith('/') else url_for('index'))


@app.errorhandler(404)
def pagina_no_encontrada(error):
    """
    H5: antes de esto, una URL mal escrita o un enlace roto mostraba la
    página de error genérica de Flask/Werkzeug (con detalle técnico si
    DEBUG está activo, o una pantalla en blanco poco amigable si no).
    """
    return render_template('errores/404.html'), 404


@app.errorhandler(403)
def acceso_prohibido(error):
    """
    H5: se dispara, por ejemplo, cuando @rol_requerido bloquea a un
    usuario autenticado pero sin el rol necesario para esa pantalla.
    """
    return render_template('errores/403.html'), 403


@app.errorhandler(500)
def error_interno(error):
    """
    H5: registra el traceback completo en el log del servidor (ver
    configuración de logging en create_app) ANTES de mostrarle al usuario
    una pantalla genérica sin detalle técnico -- Control Escolar nunca
    debe ver un traceback de Python, pero Dirección/soporte técnico sí
    necesita poder diagnosticar qué pasó revisando logs/sge.log.
    """
    app.logger.exception('Error interno no manejado: %s', error)
    db.session.rollback()  # por si el error dejó la sesión de SQLAlchemy en un estado inconsistente
    return render_template('errores/500.html'), 500



# ---------------------------------------------------------------------------
# GESTIÓN DE ADMINISTRATIVOS (solo DIRECTIVO)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# MÓDULO DE BÚSQUEDA UNIVERSAL
# Pantalla principal de uso exclusivo de Control Escolar.
# ---------------------------------------------------------------------------

@app.context_processor
def inyectar_configuracion_institucion():
    """
    Pone config_institucion disponible en TODAS las plantillas sin tener
    que pasarla a mano en cada render_template(). Esto es lo que permite
    que las plantillas digan "{{ config_institucion.nombre_periodo_singular }}"
    en vez de tener la palabra "Cuatrimestre" escrita a mano -- así el
    mismo código sirve para una universidad, una primaria, o cualquier
    otro nivel educativo, solo cambiando esta configuración.
    """
    return {'config_institucion': ConfiguracionInstitucion.obtener()}


# --- Paginación -------------------------------------------------------------
# PERFORMANCE-NOTE: con ~800 alumnos, el filtro "Activos" del buscador
# generaba una página de MÁS DE 1 MB de HTML (una tarjeta Bootstrap por
# cada alumno). El servidor la armaba rápido (~50ms gracias a los índices
# de H4), pero el navegador tiene que descargar y pintar ese megabyte:
# en una laptop modesta o por WiFi de la escuela, eso sí se siente.
# El cuello de botella no era SQL, era el tamaño de la respuesta.

@app.route('/')
@login_required
def index():
    """
    Pantalla de inicio: dashboard con números clave + filtros rápidos,
    y el buscador universal. Si viene ?filtro=algo en la URL, muestra
    esa lista filtrada en vez del dashboard vacío.
    """
    estadisticas = calcular_estadisticas_alumnos()

    filtros_disponibles = {
        'pendientes': ('Alumnos Pendientes de Validación', Alumno.estatus == EstatusAlumno.PENDIENTE),
        'activos': ('Alumnos Activos', Alumno.estatus == EstatusAlumno.ACTIVO),
        'documentacion': ('Alumnos con Documentación Pendiente', Alumno.documentacion_pendiente.isnot(None)),
        'faltas': ('Alumnos con Faltas Administrativas', Alumno.faltas_administrativas.isnot(None)),
        'con_adeudo': ('Alumnos con Adeudo Económico', None),
        'recientes': ('Últimos 10 Alumnos Registrados', None),
    }

    filtro = request.args.get('filtro')
    termino = request.args.get('q', '').strip()
    page = request.args.get('page', 1, type=int)
    resultados = None
    titulo_filtro = None
    paginacion = None

    if termino:
        # Búsqueda por texto. Llega aquí como GET (ver buscar() abajo) para
        # que los enlaces de paginación puedan conservar el término.
        consulta = Alumno.query.filter(
            or_(
                Alumno.matricula_id == termino,
                Alumno.nombre_completo.ilike(f'%{termino}%')
            )
        ).order_by(Alumno.nombre_completo.asc())

        paginacion = consulta.paginate(page=page, per_page=ALUMNOS_POR_PAGINA, error_out=False)
        resultados = paginacion.items

        if not resultados and page == 1:
            flash(f'No se encontraron alumnos que coincidan con "{termino}".', 'danger')

    elif filtro in filtros_disponibles:
        titulo_filtro, condicion = filtros_disponibles[filtro]

        if filtro == 'recientes':
            # Ya viene acotado a 10 por definición: no necesita paginarse.
            resultados = Alumno.query.order_by(Alumno.fecha_registro.desc()).limit(10).all()

        elif filtro == 'con_adeudo':
            matriculas = _matriculas_con_adeudo()
            if matriculas:
                consulta = (
                    Alumno.query
                    .filter(Alumno.matricula_id.in_(matriculas))
                    .order_by(Alumno.nombre_completo.asc())
                )
                paginacion = consulta.paginate(page=page, per_page=ALUMNOS_POR_PAGINA, error_out=False)
                resultados = paginacion.items
            else:
                resultados = []

        else:
            consulta = Alumno.query.filter(condicion).order_by(Alumno.nombre_completo.asc())
            paginacion = consulta.paginate(page=page, per_page=ALUMNOS_POR_PAGINA, error_out=False)
            resultados = paginacion.items

    return render_template(
        'buscador.html',
        estadisticas=estadisticas,
        resultados=resultados,
        titulo_filtro=titulo_filtro,
        termino=termino or None,
        filtro=filtro,
        paginacion=paginacion
    )


@app.route('/buscar', methods=['POST'])
@login_required
def buscar():
    """
    Búsqueda flexible tipo 'banco':
      - Coincidencia EXACTA por matricula_id.
      - O coincidencia PARCIAL (case-insensitive) por nombre_completo.
    """
    termino = request.form.get('termino', '').strip()

    if not termino:
        flash('Ingresa una matrícula o un nombre para buscar.', 'warning')
        return redirect(url_for('index'))

    # La búsqueda en sí vive en index(): aquí solo se redirige pasando el
    # término en la URL. Dos motivos:
    #   1. Los enlaces "Siguiente/Anterior" de la paginación son GET; si el
    #      resultado se renderizara aquí (POST), al pasar de página se
    #      perdería el término buscado.
    #   2. La URL queda compartible y se puede recargar sin que el
    #      navegador pregunte "¿reenviar formulario?".
    return redirect(url_for('index', q=termino))


# ---------------------------------------------------------------------------
# MÓDULO DE CARGA MASIVA DE ALUMNOS (Excel)
# Solo Directivo. Pensado para migrar alumnos YA inscritos con expediente
# físico, sin tener que registrarlos uno por uno desde el formulario web.
# La matrícula se genera automáticamente igual que en el registro público
# (reutiliza generar_matricula), y cada fila pasa por las MISMAS reglas
# de validación que el registro público (CURP, fechas, plan válido).
# ---------------------------------------------------------------------------

# (columna, es_obligatoria, descripción para la hoja de ayuda de la plantilla)
COLUMNAS_IMPORTACION_ALUMNOS = [
    ('nombre_completo', True, 'Nombre completo del alumno'),
    ('curp', True, 'CURP, 18 caracteres'),
    ('fecha_nacimiento', True, 'Formato AAAA-MM-DD, ej. 2005-03-21'),
    ('fecha_certificado_prepa', True, 'Formato AAAA-MM-DD'),
    ('clave_carrera', True, 'Clave del plan de estudios (ver hoja "Guía" para las claves válidas)'),
    ('sexo', True, 'Femenino / Masculino / Otro'),
    ('estatus', False, 'PENDIENTE / ACTIVO / BAJA_TEMPORAL / BAJA_DEFINITIVA / EGRESADO (vacío = ACTIVO)'),
    ('cuatrimestre_actual', False, 'Número de cuatrimestre en el que va (vacío = 1)'),
    ('correo', False, ''),
    ('telefono', False, 'Teléfono fijo'),
    ('telefono_movil', False, ''),
    ('domicilio_calle_numero', False, ''),
    ('domicilio_ciudad', False, ''),
    ('domicilio_cp', False, ''),
    ('domicilio_estado', False, ''),
    ('numero_identificacion', False, 'INE u otra identificación oficial'),
    ('estado_civil', False, ''),
    ('nacionalidad', False, 'Vacío = "Mexicana"'),
    ('tipo_sangre', False, 'Ej. O+, A-'),
    ('contacto_emergencia_nombre', False, ''),
    ('contacto_emergencia_telefono', False, ''),
    ('contacto_emergencia_parentesco', False, 'Ej. Madre, Hermana'),
    ('turno', False, 'MATUTINO / VESPERTINO / MIXTO'),
    ('modalidad', False, 'ESCOLARIZADO / SEMIESCOLARIZADO / DISTANCIA'),
    ('grupo_actual', False, ''),
]


@app.route('/alumnos/importar/plantilla')
@rol_requerido('DIRECTIVO')
def plantilla_importacion():
    """Genera y descarga el archivo .xlsx en blanco con las columnas esperadas."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Alumnos'

    fuente_encabezado = Font(bold=True, color='FFFFFF')
    relleno_obligatorio = PatternFill(start_color='0D6EFD', end_color='0D6EFD', fill_type='solid')
    relleno_opcional = PatternFill(start_color='6C757D', end_color='6C757D', fill_type='solid')

    for col_idx, (nombre_col, requerido, _desc) in enumerate(COLUMNAS_IMPORTACION_ALUMNOS, start=1):
        celda = ws.cell(row=1, column=col_idx, value=nombre_col)
        celda.font = fuente_encabezado
        celda.fill = relleno_obligatorio if requerido else relleno_opcional
        ws.column_dimensions[get_column_letter(col_idx)].width = 26

    ws.freeze_panes = 'A2'

    # --- Hoja de ayuda: qué significa cada columna + claves de carrera válidas ---
    ws_guia = wb.create_sheet('Guía')
    ws_guia.append(['Columna', 'Obligatoria', 'Descripción'])
    for celda in ws_guia[1]:
        celda.font = Font(bold=True)

    for nombre_col, requerido, desc in COLUMNAS_IMPORTACION_ALUMNOS:
        ws_guia.append([nombre_col, 'Sí' if requerido else 'No', desc])

    ws_guia.column_dimensions['A'].width = 28
    ws_guia.column_dimensions['B'].width = 12
    ws_guia.column_dimensions['C'].width = 60

    ws_guia.append([])
    fila_titulo_planes = ws_guia.max_row + 1
    ws_guia.cell(row=fila_titulo_planes, column=1, value='Claves de carrera disponibles:').font = Font(bold=True)

    for plan in PlanEstudio.query.filter_by(activo=True).order_by(PlanEstudio.clave_carrera.asc()).all():
        ws_guia.append([plan.clave_carrera, plan.nombre])

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name='plantilla_carga_masiva_alumnos.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


@app.route('/alumnos/importar', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO')
def importar_alumnos():
    if request.method == 'GET':
        return render_template('importar_alumnos.html')

    archivo = request.files.get('archivo_excel')
    if not archivo or archivo.filename == '':
        flash('Selecciona un archivo Excel (.xlsx) para importar.', 'danger')
        return redirect(url_for('importar_alumnos'))

    if not archivo.filename.lower().endswith('.xlsx'):
        flash('El archivo debe tener formato .xlsx (Excel). Usa la plantilla descargable.', 'danger')
        return redirect(url_for('importar_alumnos'))

    try:
        wb = openpyxl.load_workbook(archivo, data_only=True)
        ws = wb['Alumnos'] if 'Alumnos' in wb.sheetnames else wb.active
    except Exception:
        flash('No se pudo leer el archivo. Verifica que sea un .xlsx válido generado con la plantilla.', 'danger')
        return redirect(url_for('importar_alumnos'))

    encabezados = [(c.value.strip() if isinstance(c.value, str) else c.value) for c in ws[1]]
    nombres_columnas_conocidas = [nombre for nombre, _, _ in COLUMNAS_IMPORTACION_ALUMNOS]

    indice_columna = {
        nombre: encabezados.index(nombre)
        for nombre in nombres_columnas_conocidas
        if nombre in encabezados
    }

    faltantes = [
        nombre for nombre, requerido, _ in COLUMNAS_IMPORTACION_ALUMNOS
        if requerido and nombre not in indice_columna
    ]
    if faltantes:
        flash(
            f'Al archivo le faltan columnas obligatorias: {", ".join(faltantes)}. '
            'Descarga la plantilla más reciente y no cambies los nombres de las columnas.',
            'danger'
        )
        return redirect(url_for('importar_alumnos'))

    def valor_de(fila, nombre_col):
        idx = indice_columna.get(nombre_col)
        if idx is None or idx >= len(fila):
            return None
        valor = fila[idx].value
        if isinstance(valor, str):
            valor = valor.strip()
        return valor if valor not in ('', None) else None

    planes_por_clave = {
        p.clave_carrera: p for p in PlanEstudio.query.filter_by(activo=True).all()
    }

    exitosos = []
    errores = []

    for num_fila, fila in enumerate(ws.iter_rows(min_row=2), start=2):
        if all(c.value in (None, '') for c in fila):
            continue  # Fila completamente vacía, se omite sin generar error

        fila_errores = []

        nombre_completo = valor_de(fila, 'nombre_completo')
        curp = str(valor_de(fila, 'curp') or '').upper()
        clave_carrera = str(valor_de(fila, 'clave_carrera') or '').upper()
        sexo = valor_de(fila, 'sexo')

        if not nombre_completo or len(str(nombre_completo)) < 5:
            fila_errores.append('nombre_completo inválido o vacío')

        if not re.match(r'^[A-Z0-9]{18}$', curp):
            fila_errores.append('CURP inválida (deben ser 18 caracteres)')
        elif Alumno.query.filter_by(curp=curp).first():
            fila_errores.append(f'ya existe un alumno con la CURP {curp}')

        plan = planes_por_clave.get(clave_carrera)
        if not plan:
            fila_errores.append(f'clave_carrera "{clave_carrera}" no corresponde a ningún plan activo')

        if not sexo:
            fila_errores.append('sexo vacío')

        def parsear_fecha(nombre_columna):
            crudo = valor_de(fila, nombre_columna)
            if isinstance(crudo, datetime):
                return crudo.date(), None
            if isinstance(crudo, str):
                try:
                    return datetime.strptime(crudo, '%Y-%m-%d').date(), None
                except ValueError:
                    return None, f'{nombre_columna} con formato inválido (usa AAAA-MM-DD)'
            return None, f'{nombre_columna} vacía'

        fecha_nacimiento, error_fn = parsear_fecha('fecha_nacimiento')
        if error_fn:
            fila_errores.append(error_fn)

        fecha_certificado, error_fc = parsear_fecha('fecha_certificado_prepa')
        if error_fc:
            fila_errores.append(error_fc)

        estatus_raw = str(valor_de(fila, 'estatus') or 'ACTIVO').upper()
        if estatus_raw not in EstatusAlumno.__members__:
            fila_errores.append(f'estatus "{estatus_raw}" no es válido')

        turno = None
        turno_raw = valor_de(fila, 'turno')
        if turno_raw:
            turno_raw = str(turno_raw).upper()
            if turno_raw in TurnoAlumno.__members__:
                turno = TurnoAlumno[turno_raw]
            else:
                fila_errores.append(f'turno "{turno_raw}" no es válido')

        modalidad = None
        modalidad_raw = valor_de(fila, 'modalidad')
        if modalidad_raw:
            modalidad_raw = str(modalidad_raw).upper()
            if modalidad_raw in ModalidadEstudio.__members__:
                modalidad = ModalidadEstudio[modalidad_raw]
            else:
                fila_errores.append(f'modalidad "{modalidad_raw}" no es válida')

        cuatrimestre_raw = valor_de(fila, 'cuatrimestre_actual')
        cuatrimestre_actual = 1
        if cuatrimestre_raw is not None:
            try:
                cuatrimestre_actual = int(cuatrimestre_raw)
            except (TypeError, ValueError):
                fila_errores.append('cuatrimestre_actual debe ser un número')

        if fila_errores:
            errores.append({
                'fila': num_fila,
                'nombre': nombre_completo or '(sin nombre)',
                'errores': fila_errores,
            })
            continue

        alumno, error_creacion = crear_alumno_generando_matricula(
            plan,
            nombre_completo=nombre_completo,
            curp=curp,
            fecha_nacimiento=fecha_nacimiento,
            fecha_certificado_prepa=fecha_certificado,
            estatus=EstatusAlumno[estatus_raw],
            cuatrimestre_actual=cuatrimestre_actual,
            correo=valor_de(fila, 'correo'),
            telefono=valor_de(fila, 'telefono'),
            telefono_movil=valor_de(fila, 'telefono_movil'),
            sexo=sexo,
            numero_identificacion=valor_de(fila, 'numero_identificacion'),
            estado_civil=valor_de(fila, 'estado_civil'),
            nacionalidad=valor_de(fila, 'nacionalidad') or 'Mexicana',
            tipo_sangre=valor_de(fila, 'tipo_sangre'),
            domicilio_calle_numero=valor_de(fila, 'domicilio_calle_numero'),
            domicilio_ciudad=valor_de(fila, 'domicilio_ciudad'),
            domicilio_cp=valor_de(fila, 'domicilio_cp'),
            domicilio_estado=valor_de(fila, 'domicilio_estado'),
            contacto_emergencia_nombre=valor_de(fila, 'contacto_emergencia_nombre'),
            contacto_emergencia_telefono=valor_de(fila, 'contacto_emergencia_telefono'),
            contacto_emergencia_parentesco=valor_de(fila, 'contacto_emergencia_parentesco'),
            turno=turno,
            modalidad=modalidad,
            grupo_actual=valor_de(fila, 'grupo_actual'),
        )

        if error_creacion:
            # commit por fila individual (ver crear_alumno_generando_matricula):
            # esta falla NO afecta a las filas anteriores, que ya quedaron
            # guardadas en la base de datos de forma independiente.
            errores.append({
                'fila': num_fila,
                'nombre': nombre_completo,
                'errores': [error_creacion],
            })
            continue

        exitosos.append({'fila': num_fila, 'nombre': nombre_completo, 'matricula': alumno.matricula_id})

    if exitosos:
        flash(f'Se importaron {len(exitosos)} alumno(s) correctamente.', 'success')

    if errores:
        flash(f'{len(errores)} fila(s) no se pudieron importar (ver detalle abajo).', 'warning')

    return render_template('importar_alumnos.html', exitosos=exitosos, errores=errores)


# ---------------------------------------------------------------------------
# PERFIL DEL ESTUDIANTE / EXPEDIENTE ACADÉMICO
# ---------------------------------------------------------------------------

@app.route('/alumno/<matricula>/expediente')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def ver_expediente(matricula):
    alumno = db.get_or_404(Alumno, matricula)

    # Todas las materias del plan del alumno (el "Escudo": solo las de SU plan)
    materias_plan = (
        Materia.query
        .filter_by(id_plan_fk=alumno.id_plan_fk)
        .order_by(Materia.cuatrimestre.asc(), Materia.nombre.asc())
        .all()
    )

    # Última calificación registrada por materia (si una materia se recursó,
    # más de una Calificacion puede existir; nos quedamos con la más reciente)
    calif_por_materia = {}
    for c in sorted(alumno.calificaciones, key=lambda c: c.fecha_captura):
        calif_por_materia[c.id_materia_fk] = c

    # Agrupar por cuatrimestre para el acordeón
    cuatrimestres = {}
    for materia in materias_plan:
        cuatrimestres.setdefault(materia.cuatrimestre, []).append({
            'materia': materia,
            'calificacion': calif_por_materia.get(materia.id)
        })

    # Materias del plan que aún no están aprobadas (calificación >= 6).
    # Se usa para advertir (sin bloquear) al intentar marcar al alumno como Egresado.
    materias_no_aprobadas = [
        materia for materia in materias_plan
        if not (calif_por_materia.get(materia.id) and calif_por_materia[materia.id].calificacion_final >= 6)
    ]

    return render_template(
        'expediente.html',
        alumno=alumno,
        cuatrimestres=cuatrimestres,
        max_cuatrimestres=_max_periodos(),
        estatus_disponibles=list(EstatusAlumno),
        materias_no_aprobadas=materias_no_aprobadas,
        historial_estatus=alumno.historial_estatus
    )


@app.route('/alumno/<matricula>/ficha')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def ficha_inscripcion(matricula):
    """
    Ficha de Inscripción imprimible (frente + reverso), a partir de los
    datos ya capturados en el registro público y el módulo de documentos.
    No requiere formulario propio: es una vista de solo lectura para
    imprimir/archivar, con checklist de documentos recibidos en el reverso.
    """
    alumno = db.get_or_404(Alumno, matricula)

    documentos_alumno = DocumentoAlumno.query.filter_by(matricula_fk=matricula).all()
    documentos_subidos = {doc.tipo_documento for doc in documentos_alumno}
    foto_alumno = next((doc for doc in documentos_alumno if doc.tipo_documento == TipoDocumento.FOTOGRAFIA), None)

    return render_template(
        'ficha_inscripcion.html',
        alumno=alumno,
        tipos_documento=list(TipoDocumento),
        documentos_subidos=documentos_subidos,
        foto_alumno=foto_alumno,
    )


@app.route('/alumno/<matricula>/cambiar-estatus', methods=['POST'])
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def cambiar_estatus(matricula):
    """
    Cambia el estatus del alumno (ej. Pendiente -> Activo -> Egresado).
    Registra el cambio en HistorialEstatus para auditoría. Si se intenta
    marcar como EGRESADO y el alumno tiene materias sin aprobar, se pide
    un comentario justificando el motivo (advertencia, no bloqueo total).
    """
    alumno = db.get_or_404(Alumno, matricula)
    nuevo_estatus_raw = request.form.get('nuevo_estatus', '')
    comentario = request.form.get('comentario', '').strip() or None

    if nuevo_estatus_raw not in EstatusAlumno.__members__:
        flash('El estatus indicado no es válido.', 'danger')
        return redirect(url_for('ver_expediente', matricula=matricula))

    nuevo_estatus = EstatusAlumno[nuevo_estatus_raw]

    if nuevo_estatus == alumno.estatus:
        flash('El alumno ya tiene ese estatus; no se realizó ningún cambio.', 'warning')
        return redirect(url_for('ver_expediente', matricula=matricula))

    if nuevo_estatus == EstatusAlumno.EGRESADO:
        materias_plan = Materia.query.filter_by(id_plan_fk=alumno.id_plan_fk).all()
        calif_por_materia = {c.id_materia_fk: c for c in alumno.calificaciones}
        faltantes = [
            m for m in materias_plan
            if not (calif_por_materia.get(m.id) and calif_por_materia[m.id].calificacion_final >= 6)
        ]

        if faltantes and not comentario:
            nombres = ', '.join(m.nombre for m in faltantes[:5])
            extra = f' y {len(faltantes) - 5} más' if len(faltantes) > 5 else ''
            flash(
                f'El alumno tiene {len(faltantes)} materia(s) sin aprobar ({nombres}{extra}). '
                'Si de verdad quieres marcarlo como Egresado, agrega un comentario '
                'explicando el motivo y vuelve a intentarlo.',
                'warning'
            )
            return redirect(url_for('ver_expediente', matricula=matricula))

        if alumno.tiene_adeudo() and not comentario:
            saldo = alumno.saldo_total_adeudado()
            flash(
                f'El alumno tiene un adeudo económico de ${saldo:.2f}. '
                'Si de verdad quieres marcarlo como Egresado, agrega un comentario '
                'explicando el motivo (ver Cobros para el detalle) y vuelve a intentarlo.',
                'warning'
            )
            return redirect(url_for('ver_expediente', matricula=matricula))

    registro = HistorialEstatus(
        matricula_fk=alumno.matricula_id,
        usuario_fk=current_user.id,
        estatus_anterior=alumno.estatus,
        estatus_nuevo=nuevo_estatus,
        comentario=comentario,
    )
    alumno.estatus = nuevo_estatus

    cargos_auto_generados = []
    materias_auto_inscritas = []
    avisos_de_configuracion = []
    if nuevo_estatus == EstatusAlumno.ACTIVO and not alumno.fecha_validacion:
        alumno.fecha_validacion = ahora_utc()
        cargos_auto_generados, avisos_de_configuracion = _generar_cargos_de_inscripcion(alumno)
        materias_auto_inscritas = _generar_carga_academica(alumno)

    db.session.add(registro)
    try:
        db.session.commit()
    except IntegrityError:
        # CONCURRENCIA: _cargo_duplicado() ya se revisó arriba (dentro de
        # _generar_cargos_de_inscripcion()), pero eso es solo un check en
        # Python -- la garantía real es el índice único de la BD (ver
        # migración b7e2c9a41f3d). Si dos peticiones activan al mismo
        # alumno casi al mismo tiempo, una gana y la otra cae aquí.
        db.session.rollback()
        flash(
            'No se pudo actualizar el estatus: ya se generó un cargo con la misma '
            'clave (alumno + concepto + periodo) justo ahora, probablemente por '
            'otra operación al mismo tiempo. Vuelve a intentarlo.',
            'danger'
        )
        return redirect(url_for('ver_expediente', matricula=matricula))

    flash(f'Estatus del alumno actualizado a "{nuevo_estatus.value}".', 'success')
    if cargos_auto_generados:
        flash(
            f'Se generaron automáticamente {len(cargos_auto_generados)} cargo(s) de inscripción: '
            f'{", ".join(c.periodo_escolar for c in cargos_auto_generados)}. '
            'Revísalos en Cobros.',
            'success'
        )
    if materias_auto_inscritas:
        flash(
            f'Se generó su carga académica del {alumno.cuatrimestre_actual}° cuatrimestre '
            f'({len(materias_auto_inscritas)} materia(s)). Puedes verla en Carga Académica.',
            'success'
        )
    # Falta configuración de precios: no se inventa un monto, se dice qué falta.
    for aviso in avisos_de_configuracion:
        flash(aviso, 'warning')
    return redirect(url_for('ver_expediente', matricula=matricula))


@app.route('/alumno/<matricula>/avanzar-cuatrimestre', methods=['POST'])
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def avanzar_cuatrimestre(matricula):
    """Avanza a UN alumno al siguiente cuatrimestre -- para casos sueltos (ej. alguien que regresó de Baja Temporal)."""
    alumno = db.get_or_404(Alumno, matricula)

    ok, mensaje, cargos, materias, avisos_de_configuracion = _avanzar_cuatrimestre(alumno)

    if ok:
        try:
            db.session.commit()
        except IntegrityError:
            # CONCURRENCIA: mismo caso que cambiar_estatus() -- ver migración
            # b7e2c9a41f3d.
            db.session.rollback()
            flash(
                'No se pudo avanzar de cuatrimestre: ya se generó un cargo con la '
                'misma clave (alumno + concepto + periodo) justo ahora, '
                'probablemente por otra operación al mismo tiempo. Vuelve a '
                'intentarlo.',
                'danger'
            )
            return redirect(url_for('ver_expediente', matricula=matricula))
        flash(mensaje, 'success')
        if cargos:
            flash(f'Se generaron {len(cargos)} cargo(s) nuevo(s). Revísalos en Cobros.', 'success')
        if materias:
            flash(f'Se generó su carga académica del {alumno.cuatrimestre_actual}° cuatrimestre ({len(materias)} materia(s)).', 'success')
        for aviso in avisos_de_configuracion:
            flash(aviso, 'warning')
    else:
        db.session.rollback()
        flash(mensaje, 'danger')

    return redirect(url_for('ver_expediente', matricula=matricula))


# ---------------------------------------------------------------------------
# SISTEMA DE COBROS
# Ver y consultar: ambos roles. Crear/cancelar cargos: solo Directivo
# (define la estructura financiera). Registrar un pago: ambos roles (es
# el trabajo diario de Control Escolar en la ventanilla — "actualizar").
# ---------------------------------------------------------------------------

@app.route('/alumno/<matricula>/cobros')
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


@app.route('/alumno/<matricula>/becas', methods=['GET', 'POST'])
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
            return redirect(url_for('becas_alumno', matricula=matricula))

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

        return redirect(url_for('becas_alumno', matricula=matricula))

    becas = Beca.query.filter_by(matricula_fk=matricula).order_by(Beca.fecha_otorgada.desc()).all()
    return render_template(
        'becas_alumno.html',
        alumno=alumno,
        becas=becas,
        tipos_descuento=list(TipoDescuentoBeca),
        periodo_escolar_sugerido=periodo_escolar_actual(),
    )


@app.route('/becas/<int:beca_id>/desactivar', methods=['POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def desactivar_beca(beca_id):
    """Desactiva una beca -- los cargos que ya se generaron con el descuento NO se revierten automáticamente."""
    beca = db.get_or_404(Beca, beca_id)
    beca.activa = False
    db.session.commit()
    flash(f'Beca "{beca.nombre}" desactivada. Los cargos ya generados con ese descuento no se revierten solos.', 'warning')
    return redirect(url_for('becas_alumno', matricula=beca.matricula_fk))


@app.route('/alumno/<matricula>/cobros/nuevo', methods=['POST'])
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
        return redirect(url_for('cobros', matricula=matricula))

    duplicado = _cargo_duplicado(alumno.matricula_id, concepto_cobro.id, periodo_escolar)
    if duplicado:
        flash(
            f'Ya existe un cargo de "{concepto_cobro.nombre}"'
            f'{" para el periodo " + periodo_escolar if periodo_escolar else ""} '
            f'sin cancelar (estatus: {duplicado.estatus.value}, folio interno #{duplicado.id}). '
            'Si de verdad necesitas otro, cancela primero el existente o usa un periodo distinto.',
            'warning'
        )
        return redirect(url_for('cobros', matricula=matricula))

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
        return redirect(url_for('cobros', matricula=matricula))

    flash(f'Cargo "{concepto_cobro.nombre}" agregado correctamente.', 'success')
    return redirect(url_for('cobros', matricula=matricula))


@app.route('/cobro/<int:cargo_id>/pagar', methods=['POST'])
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
        return redirect(url_for('cobros', matricula=cargo.matricula_fk))

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
        return redirect(url_for('cobros', matricula=cargo.matricula_fk))

    saldo = cargo.saldo_pendiente()
    if monto_pagado > saldo:
        flash(
            f'El monto pagado (${monto_pagado}) es mayor al saldo pendiente (${saldo}). '
            'Verifica el monto.',
            'danger'
        )
        return redirect(url_for('cobros', matricula=cargo.matricula_fk))

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

    return redirect(url_for('cobros', matricula=cargo.matricula_fk))


@app.route('/pago/<int:pago_id>/anular', methods=['POST'])
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
        return redirect(url_for('cobros', matricula=cargo.matricula_fk))

    motivo = request.form.get('motivo_anulacion', '').strip()
    if len(motivo) < 5:
        flash('Indica un motivo (mínimo 5 caracteres) para anular este pago.', 'danger')
        return redirect(url_for('cobros', matricula=cargo.matricula_fk))

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
    return redirect(url_for('cobros', matricula=cargo.matricula_fk))


@app.route('/cobro/<int:cargo_id>/cancelar', methods=['POST'])
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
        return redirect(url_for('cobros', matricula=cargo.matricula_fk))

    cargo.estatus = EstatusCargo.CANCELADO
    cargo.comentario = comentario
    db.session.commit()

    flash(f'Cargo "{cargo.concepto}" cancelado.', 'success')
    return redirect(url_for('cobros', matricula=cargo.matricula_fk))


@app.route('/cobro/<int:cargo_id>/condonar-recargo', methods=['POST'])
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
        return redirect(url_for('cobros', matricula=cargo.matricula_fk))

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
        return redirect(url_for('cobros', matricula=cargo.matricula_fk))

    anterior = cargo.recargo_aplicado
    cargo.recargo_aplicado = nuevo_recargo
    cargo.recargo_congelado = True
    cargo.comentario = f'Recargo condonado por {current_user.nombre_completo}: ${anterior} -> ${nuevo_recargo}. Motivo: {motivo}'
    cargo.actualizar_estatus()
    db.session.commit()

    flash(f'Recargo de "{cargo.concepto}" ajustado de ${anterior} a ${nuevo_recargo}.', 'success')
    return redirect(url_for('cobros', matricula=cargo.matricula_fk))


@app.route('/pago/<int:pago_id>/recibo')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')
def recibo_pago(pago_id):
    pago = db.get_or_404(Pago, pago_id)
    return render_template('recibo_pago.html', pago=pago, cargo=pago.cargo, alumno=pago.cargo.alumno)


@app.route('/alumno/<matricula>/estado-cuenta')
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


@app.route('/reportes/cobros-del-dia')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')
def reporte_cobros_del_dia():
    """
    Reporte a demanda de todos los pagos capturados en una fecha (por
    defecto, hoy). Cualquiera de los dos roles puede consultarlo —
    es información, no una acción de modificación.
    """
    fecha_raw = request.args.get('fecha', '')
    try:
        fecha_reporte = datetime.strptime(fecha_raw, '%Y-%m-%d').date() if fecha_raw else hoy_local()
    except ValueError:
        fecha_reporte = hoy_local()
        flash('La fecha indicada no era válida; se muestra el día de hoy.', 'warning')

    pagos_del_dia, total_del_dia, totales_por_concepto, totales_por_metodo = _calcular_reporte_cobros_del_dia(fecha_reporte)

    return render_template(
        'reporte_cobros_dia.html',
        fecha_reporte=fecha_reporte,
        pagos_del_dia=pagos_del_dia,
        total_del_dia=total_del_dia,
        totales_por_concepto=totales_por_concepto,
        totales_por_metodo=totales_por_metodo,
    )


@app.route('/reportes/cobros-del-dia/exportar')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')
def exportar_reporte_cobros_del_dia():
    fecha_raw = request.args.get('fecha', '')
    try:
        fecha_reporte = datetime.strptime(fecha_raw, '%Y-%m-%d').date() if fecha_raw else hoy_local()
    except ValueError:
        fecha_reporte = hoy_local()

    pagos_del_dia, total_del_dia, totales_por_concepto, totales_por_metodo = _calcular_reporte_cobros_del_dia(fecha_reporte)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Cobros del Día'

    fuente_encabezado = Font(bold=True, color='FFFFFF')
    relleno_encabezado = PatternFill(start_color='0D6EFD', end_color='0D6EFD', fill_type='solid')

    ws.append([f'Reporte de Cobros del Día - {fecha_reporte.strftime("%d/%m/%Y")}'])
    ws['A1'].font = Font(bold=True, size=14)
    ws.append([])
    ws.append(['Folio', 'Hora', 'Alumno', 'Matrícula', 'Concepto', 'Método', 'Monto'])
    for celda in ws[3]:
        celda.font = fuente_encabezado
        celda.fill = relleno_encabezado

    for pago in pagos_del_dia:
        ws.append([
            pago.folio or f'#{pago.id}',
            pago.fecha_pago.strftime('%H:%M'),
            pago.cargo.alumno.nombre_completo if pago.cargo and pago.cargo.alumno else '—',
            pago.cargo.matricula_fk if pago.cargo else '—',
            pago.cargo.concepto if pago.cargo else '—',
            pago.metodo_pago.value,
            float(pago.monto_pagado),
        ])

    ws.append([])
    fila_total = ws.max_row + 1
    ws.cell(row=fila_total, column=6, value='TOTAL:').font = Font(bold=True)
    ws.cell(row=fila_total, column=7, value=float(total_del_dia)).font = Font(bold=True)

    for col in 'ABCDEFG':
        ws.column_dimensions[col].width = 20

    ws_concepto = wb.create_sheet('Por Concepto')
    ws_concepto.append(['Concepto', 'Total'])
    for celda in ws_concepto[1]:
        celda.font = fuente_encabezado
        celda.fill = relleno_encabezado
    for concepto, total in totales_por_concepto.items():
        ws_concepto.append([concepto, float(total)])
    ws_concepto.column_dimensions['A'].width = 30
    ws_concepto.column_dimensions['B'].width = 15

    ws_metodo = wb.create_sheet('Por Método de Pago')
    ws_metodo.append(['Método', 'Total'])
    for celda in ws_metodo[1]:
        celda.font = fuente_encabezado
        celda.fill = relleno_encabezado
    for metodo, total in totales_por_metodo.items():
        ws_metodo.append([metodo, float(total)])
    ws_metodo.column_dimensions['A'].width = 25
    ws_metodo.column_dimensions['B'].width = 15

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name=f'cobros_del_dia_{fecha_reporte.isoformat()}.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


@app.route('/reportes/cartera-vencida')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')
def cartera_vencida():
    """
    Lista de cargos vencidos (pendientes o parciales, con fecha de
    vencimiento ya pasada), ordenados por saldo pendiente de mayor a
    menor -- para priorizar a quién darle seguimiento de cobranza primero.
    Recalcula el recargo de cada uno antes de mostrar, igual que /cobros.
    """
    filas, total_vencido = _calcular_cartera_vencida()

    # La lista se pagina en Python (no en SQL) porque el orden es por saldo
    # pendiente, que es un valor calculado, no una columna. total_vencido y
    # total_filas siguen siendo los GLOBALES: las tarjetas de resumen deben
    # mostrar la cartera completa, no solo lo que se ve en esta página.
    page = request.args.get('page', 1, type=int)
    filas_pagina, paginacion = _paginar_lista(filas, page, CARGOS_POR_PAGINA)

    return render_template(
        'cartera_vencida.html',
        filas=filas_pagina,
        total_vencido=total_vencido,
        total_filas=len(filas),
        paginacion=paginacion
    )


@app.route('/reportes/cartera-vencida/exportar')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')
def exportar_cartera_vencida():
    filas, total_vencido = _calcular_cartera_vencida()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Cartera Vencida'

    fuente_encabezado = Font(bold=True, color='FFFFFF')
    relleno_encabezado = PatternFill(start_color='DC3545', end_color='DC3545', fill_type='solid')

    ws.append([f'Cartera Vencida - Generado {ahora_utc().strftime("%d/%m/%Y %H:%M")}'])
    ws['A1'].font = Font(bold=True, size=14)
    ws.append([])
    ws.append(['Alumno', 'Matrícula', 'Concepto', 'Periodo', 'Días de Atraso', 'Saldo Pendiente'])
    for celda in ws[3]:
        celda.font = fuente_encabezado
        celda.fill = relleno_encabezado

    for fila in filas:
        cargo = fila['cargo']
        ws.append([
            cargo.alumno.nombre_completo,
            cargo.matricula_fk,
            cargo.concepto,
            cargo.periodo_escolar or '—',
            fila['dias_atraso'],
            float(fila['saldo']),
        ])

    ws.append([])
    fila_total = ws.max_row + 1
    ws.cell(row=fila_total, column=5, value='TOTAL VENCIDO:').font = Font(bold=True)
    ws.cell(row=fila_total, column=6, value=float(total_vencido)).font = Font(bold=True)

    for col, ancho in zip('ABCDEF', [30, 15, 25, 15, 15, 18]):
        ws.column_dimensions[col].width = ancho

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name=f'cartera_vencida_{ahora_utc().date().isoformat()}.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


@app.route('/cobros/dashboard')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')
def dashboard_cobros():
    datos = _calcular_dashboard_cobros()
    return render_template('dashboard_cobros.html', **datos)


@app.route('/cobros/dashboard/exportar')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')
def exportar_dashboard_cobros():
    datos = _calcular_dashboard_cobros()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Resumen'

    fuente_encabezado = Font(bold=True, color='FFFFFF')
    relleno_encabezado = PatternFill(start_color='0D6EFD', end_color='0D6EFD', fill_type='solid')

    ws.append([f'Dashboard de Cobros - Generado {ahora_utc().strftime("%d/%m/%Y %H:%M")}'])
    ws['A1'].font = Font(bold=True, size=14)
    ws.append([])
    ws.append(['Total cobrado este mes', float(datos['total_cobrado_mes_actual'])])
    ws.append(['Total cobrado (últimos 6 meses)', float(datos['total_cobrado_6_meses'])])
    ws.append(['Total por cobrar (saldo abierto)', float(datos['total_por_cobrar'])])
    ws.append(['Total vencido', float(datos['total_vencido'])])
    ws.column_dimensions['A'].width = 32
    ws.column_dimensions['B'].width = 18

    ws_mensual = wb.create_sheet('Ingresos Mensuales')
    ws_mensual.append(['Mes', 'Total Cobrado'])
    for celda in ws_mensual[1]:
        celda.font = fuente_encabezado
        celda.fill = relleno_encabezado
    for item in datos['ingresos_mensuales']:
        ws_mensual.append([item['etiqueta'], float(item['total'])])
    ws_mensual.column_dimensions['A'].width = 18
    ws_mensual.column_dimensions['B'].width = 18

    ws_concepto = wb.create_sheet('Ingresos por Concepto')
    ws_concepto.append(['Concepto', 'Total (últimos 6 meses)'])
    for celda in ws_concepto[1]:
        celda.font = fuente_encabezado
        celda.fill = relleno_encabezado
    for item in datos['ingresos_por_concepto']:
        ws_concepto.append([item['concepto'], float(item['total'])])
    ws_concepto.column_dimensions['A'].width = 30
    ws_concepto.column_dimensions['B'].width = 20

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name=f'dashboard_cobros_{ahora_utc().date().isoformat()}.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


@app.route('/cobros/generar-mensualidades', methods=['GET', 'POST'])
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


@app.route('/alumnos/avanzar-cuatrimestre-lote', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO')
def avanzar_cuatrimestre_lote():
    """
    Avanza de golpe a TODOS los alumnos Activos de una carrera+cuatrimestre
    al siguiente cuatrimestre -- pensado para el cambio de periodo, cuando
    todo un grupo pasa junto. Reutiliza _avanzar_cuatrimestre(), el mismo
    helper que la versión individual, así que genera exactamente los
    mismos cargos y carga académica, solo que para muchos a la vez.
    """
    planes = PlanEstudio.query.filter_by(activo=True).order_by(PlanEstudio.nombre.asc()).all()
    max_cuatri = _max_periodos()

    if request.method == 'POST':
        plan_id = request.form.get('plan_id', type=int)
        cuatrimestre_actual = request.form.get('cuatrimestre_actual', type=int)

        plan = db.session.get(PlanEstudio, plan_id) if plan_id else None
        if not plan or not cuatrimestre_actual:
            flash('Selecciona carrera y el cuatrimestre en el que están ahora.', 'danger')
            return render_template('avanzar_cuatrimestre.html', planes=planes, max_cuatrimestres=max_cuatri)

        alumnos = (
            Alumno.query
            .filter_by(id_plan_fk=plan.id, cuatrimestre_actual=cuatrimestre_actual, estatus=EstatusAlumno.ACTIVO)
            .order_by(Alumno.nombre_completo.asc())
            .all()
        )

        avanzados = []
        omitidos = []
        # Configuración de precios que falta. Se junta SIN repetir: si a 40
        # alumnos les falta el mismo precio, es un solo problema de
        # configuración, no 40 avisos en pantalla.
        avisos_de_configuracion = []
        for alumno in alumnos:
            ok, mensaje, cargos, materias, avisos = _avanzar_cuatrimestre(alumno)
            if ok:
                avanzados.append({'alumno': alumno, 'cargos': len(cargos), 'materias': len(materias)})
            else:
                omitidos.append({'alumno': alumno, 'motivo': mensaje})
            for aviso in avisos:
                if aviso not in avisos_de_configuracion:
                    avisos_de_configuracion.append(aviso)

        try:
            db.session.commit()
        except IntegrityError:
            # CONCURRENCIA: mismo caso que generar_mensualidades() -- todo el
            # lote es una sola transacción; si algún cargo choca con el
            # índice único (migración b7e2c9a41f3d) se descarta el lote
            # COMPLETO (nunca a medias) y se pide reintentar.
            db.session.rollback()
            flash(
                'No se aplicó el avance de cuatrimestre: alguno de estos cargos '
                'ya se generó justo ahora por otra operación. Vuelve a '
                'intentarlo -- los que ya estén al día se omitirán solos.',
                'danger'
            )
            return render_template('avanzar_cuatrimestre.html', planes=planes, max_cuatrimestres=max_cuatri)

        flash(
            f'{len(avanzados)} alumno(s) avanzaron al {cuatrimestre_actual + 1}° cuatrimestre. '
            f'{len(omitidos)} se omitieron (ver detalle abajo).',
            'success' if avanzados else 'warning'
        )
        for aviso in avisos_de_configuracion:
            flash(aviso, 'warning')
        return render_template(
            'avanzar_cuatrimestre.html',
            planes=planes,
            max_cuatrimestres=max_cuatri,
            avanzados=avanzados,
            omitidos=omitidos,
        )

    return render_template('avanzar_cuatrimestre.html', planes=planes, max_cuatrimestres=max_cuatri)


@app.route('/cobros/recordatorios-vencimiento', methods=['GET', 'POST'])
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


@app.route('/planes/mensualidades', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO')
def planes_mensualidades():
    """
    Pantalla mínima para que Dirección ajuste el precio de mensualidad de
    cada carrera (cada una puede costar distinto). Solo edita ese campo;
    el CRUD completo de Planes de Estudio sigue pendiente como tarea aparte.
    """
    if request.method == 'POST':
        plan_id = request.form.get('plan_id', '').strip()
        monto_raw = request.form.get('monto_mensualidad', '').strip()
        plan = db.get_or_404(PlanEstudio, int(plan_id)) if plan_id.isdigit() else None

        if not plan:
            flash('Plan de estudios no encontrado.', 'danger')
        elif not monto_raw:
            # Campo vacío = "No definido" a propósito (así lo indica el
            # placeholder del formulario). Antes esto siempre fallaba la
            # validación de Decimal('') y nunca se podía volver a dejar sin
            # definir una mensualidad ya configurada.
            plan.monto_mensualidad = None
            db.session.commit()
            flash(f'Mensualidad de "{plan.nombre}" eliminada (queda sin definir).', 'success')
        else:
            try:
                monto = Decimal(monto_raw)
                if monto < 0:
                    raise InvalidOperation
                plan.monto_mensualidad = monto
                db.session.commit()
                flash(f'Mensualidad de "{plan.nombre}" actualizada a ${monto}.', 'success')
            except InvalidOperation:
                flash('El monto no es un número válido.', 'danger')

        return redirect(url_for('planes_mensualidades'))

    planes = PlanEstudio.query.order_by(PlanEstudio.nombre.asc()).all()
    return render_template('planes_mensualidades.html', planes=planes)


@app.route('/planes/<int:plan_id>/materias', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO')
def gestionar_materias(plan_id):
    """
    Alta manual de materias por cuatrimestre para un plan de estudios.
    Solo Directivo: esto es lo que define el "Escudo del Plan de
    Estudios" que todo lo demás (boletas, cargos de mensualidad, carga
    académica) respeta -- agregar una materia aquí por error se propaga
    a todo el sistema, así que queda con el rol más restringido.
    """
    plan = db.get_or_404(PlanEstudio, plan_id)
    max_cuatri = _max_periodos()

    if request.method == 'POST':
        nombre = request.form.get('nombre', '').strip()
        clave = request.form.get('clave', '').strip() or None
        cuatrimestre_raw = request.form.get('cuatrimestre', '').strip()
        creditos_raw = request.form.get('creditos', '').strip()

        errores = []
        if len(nombre) < 3:
            errores.append('El nombre de la materia debe tener al menos 3 caracteres.')

        cuatrimestre = None
        try:
            cuatrimestre = int(cuatrimestre_raw)
            if cuatrimestre < 1 or cuatrimestre > max_cuatri:
                errores.append(f'El cuatrimestre debe estar entre 1 y {max_cuatri}.')
                cuatrimestre = None
        except (ValueError, TypeError):
            errores.append('Indica un número de cuatrimestre válido.')

        creditos = None
        if creditos_raw:
            try:
                creditos = float(creditos_raw)
            except ValueError:
                errores.append('Los créditos deben ser un número.')

        if not errores:
            duplicada = Materia.query.filter_by(id_plan_fk=plan.id, nombre=nombre, cuatrimestre=cuatrimestre).first()
            if duplicada:
                errores.append(f'Ya existe "{nombre}" en el {cuatrimestre}° cuatrimestre de este plan.')

        if errores:
            for error in errores:
                flash(error, 'danger')
        else:
            db.session.add(Materia(nombre=nombre, clave=clave, cuatrimestre=cuatrimestre, creditos=creditos, id_plan_fk=plan.id))
            db.session.commit()
            flash(f'"{nombre}" agregada al {cuatrimestre}° cuatrimestre de {plan.nombre}.', 'success')

        return redirect(url_for('gestionar_materias', plan_id=plan.id))

    materias_por_cuatrimestre = {}
    for materia in Materia.query.filter_by(id_plan_fk=plan.id).order_by(Materia.cuatrimestre.asc(), Materia.nombre.asc()).all():
        materias_por_cuatrimestre.setdefault(materia.cuatrimestre, []).append(materia)

    return render_template(
        'gestionar_materias.html',
        plan=plan,
        materias_por_cuatrimestre=materias_por_cuatrimestre,
        max_cuatrimestres=max_cuatri,
    )


@app.route('/materias/<int:materia_id>/eliminar', methods=['POST'])
@rol_requerido('DIRECTIVO')
def eliminar_materia(materia_id):
    """
    Solo permite eliminar una materia si NUNCA se usó -- ni calificaciones
    ni carga académica registradas con ella. Igual que con Cargo, no se
    permite borrar algo que ya dejó huella en el sistema.
    """
    materia = db.get_or_404(Materia, materia_id)
    plan_id = materia.id_plan_fk

    tiene_calificaciones = Calificacion.query.filter_by(id_materia_fk=materia.id).first() is not None
    tiene_inscripciones = InscripcionMateria.query.filter_by(id_materia_fk=materia.id).first() is not None

    if tiene_calificaciones or tiene_inscripciones:
        flash(f'No se puede eliminar "{materia.nombre}": ya tiene calificaciones o carga académica registradas.', 'danger')
    else:
        nombre = materia.nombre
        db.session.delete(materia)
        db.session.commit()
        flash(f'"{nombre}" eliminada del plan.', 'success')

    return redirect(url_for('gestionar_materias', plan_id=plan_id))


@app.route('/conceptos-cobro', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def conceptos_cobro():
    """Catálogo de conceptos de cobro — se administra aquí, NUNCA como texto libre al capturar un cargo."""
    if request.method == 'POST':
        nombre = request.form.get('nombre', '').strip()
        monto_sugerido_raw = request.form.get('monto_sugerido', '').strip()
        es_mensualidad = request.form.get('es_mensualidad') == 'on'

        monto_sugerido = None
        if monto_sugerido_raw:
            try:
                monto_sugerido = Decimal(monto_sugerido_raw)
                if monto_sugerido < 0:
                    raise InvalidOperation
            except InvalidOperation:
                flash('El precio sugerido no es un número válido.', 'danger')
                return redirect(url_for('conceptos_cobro'))

        if len(nombre) < 3:
            flash('El nombre del concepto debe tener al menos 3 caracteres.', 'danger')
        elif ConceptoCobro.query.filter_by(nombre=nombre).first():
            flash(f'Ya existe un concepto llamado "{nombre}".', 'danger')
        else:
            nuevo = ConceptoCobro(nombre=nombre, monto_sugerido=monto_sugerido, es_mensualidad=es_mensualidad, activo=True)
            db.session.add(nuevo)
            db.session.commit()
            flash(f'Concepto "{nombre}" agregado al catálogo.', 'success')

        return redirect(url_for('conceptos_cobro'))

    conceptos = ConceptoCobro.query.order_by(ConceptoCobro.activo.desc(), ConceptoCobro.nombre.asc()).all()
    return render_template('conceptos_cobro.html', conceptos=conceptos)


@app.route('/conceptos-cobro/<int:concepto_id>/editar-precio', methods=['POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def editar_precio_concepto(concepto_id):
    """
    Ajustar el precio sugerido de un concepto YA existente -- pensado
    para cuando cambian las cuotas cada año (algo poco frecuente, no
    necesita un CRUD completo, solo poder tocar el número).
    """
    concepto = db.get_or_404(ConceptoCobro, concepto_id)
    monto_sugerido_raw = request.form.get('monto_sugerido', '').strip()
    es_mensualidad = request.form.get('es_mensualidad') == 'on'

    monto_sugerido = None
    if monto_sugerido_raw:
        try:
            monto_sugerido = Decimal(monto_sugerido_raw)
            if monto_sugerido < 0:
                raise InvalidOperation
        except InvalidOperation:
            flash('El precio sugerido no es un número válido.', 'danger')
            return redirect(url_for('conceptos_cobro'))

    concepto.monto_sugerido = monto_sugerido
    concepto.es_mensualidad = es_mensualidad
    db.session.commit()

    flash(f'Precio de "{concepto.nombre}" actualizado.', 'success')
    return redirect(url_for('conceptos_cobro'))


@app.route('/conceptos-cobro/<int:concepto_id>/toggle', methods=['POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def toggle_concepto_cobro(concepto_id):
    concepto = db.get_or_404(ConceptoCobro, concepto_id)
    concepto.activo = not concepto.activo
    db.session.commit()

    estado = 'activado' if concepto.activo else 'desactivado'
    flash(f'El concepto "{concepto.nombre}" fue {estado}.', 'success')
    return redirect(url_for('conceptos_cobro'))


@app.route('/configuracion/institucion', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO')
def configuracion_institucion():
    """
    Pantalla donde Directivo configura cómo se llama la institución, cómo
    se le llama a cada periodo (Cuatrimestre / Grado / Semestre / Año /
    Trimestre...) y al programa que los agrupa (Carrera / Nivel Educativo
    / Grado Escolar...). Esto es lo que hace que el sistema sirva para
    cualquier tipo de escuela sin tocar código -- la estructura de datos
    de por sí ya es genérica, solo cambia cómo se le llama a cada cosa.
    """
    config = ConfiguracionInstitucion.obtener()

    if request.method == 'POST':
        nombre_institucion = request.form.get('nombre_institucion', '').strip()
        nombre_periodo_singular = request.form.get('nombre_periodo_singular', '').strip()
        nombre_periodo_plural = request.form.get('nombre_periodo_plural', '').strip()
        nombre_programa_singular = request.form.get('nombre_programa_singular', '').strip()
        nombre_programa_plural = request.form.get('nombre_programa_plural', '').strip()
        max_periodos_raw = request.form.get('max_periodos', '').strip()

        errores = []
        if len(nombre_institucion) < 3:
            errores.append('El nombre de la institución debe tener al menos 3 caracteres.')
        if not nombre_periodo_singular or not nombre_periodo_plural:
            errores.append('Indica cómo se llama cada periodo, en singular y en plural (ej. "Cuatrimestre" / "Cuatrimestres").')
        if not nombre_programa_singular or not nombre_programa_plural:
            errores.append('Indica cómo se llama el programa que agrupa los periodos, en singular y en plural (ej. "Carrera" / "Carreras").')

        max_periodos = None
        try:
            max_periodos = int(max_periodos_raw)
            if max_periodos < 1 or max_periodos > 30:
                errores.append('El número máximo de periodos debe estar entre 1 y 30.')
        except (ValueError, TypeError):
            errores.append('Indica un número máximo de periodos válido.')

        if errores:
            for error in errores:
                flash(error, 'danger')
        else:
            config.nombre_institucion = nombre_institucion
            config.nombre_periodo_singular = nombre_periodo_singular
            config.nombre_periodo_plural = nombre_periodo_plural
            config.nombre_programa_singular = nombre_programa_singular
            config.nombre_programa_plural = nombre_programa_plural
            config.max_periodos = max_periodos
            db.session.commit()
            flash('Configuración de la institución actualizada.', 'success')

        return redirect(url_for('configuracion_institucion'))

    return render_template('configuracion_institucion.html', config=config)


@app.route('/configuracion/cobros', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def configuracion_cobros():
    """
    Configuración de recargos por atraso — auto-ajustable: cada
    universidad define su propia fórmula aquí, sin tocar código.
    """
    config = ConfiguracionCobros.obtener()

    if request.method == 'POST':
        tipo_raw = request.form.get('tipo_recargo', '')
        valor_raw = request.form.get('valor_recargo', '').strip()
        dias_gracia_raw = request.form.get('dias_gracia', '0').strip()

        errores = []

        if tipo_raw not in TipoRecargo.__members__:
            errores.append('Selecciona un tipo de recargo válido.')

        valor = None
        try:
            valor = Decimal(valor_raw)
            if valor < 0:
                errores.append('El valor del recargo no puede ser negativo.')
        except InvalidOperation:
            errores.append('El valor del recargo no es un número válido.')

        try:
            dias_gracia = int(dias_gracia_raw)
            if dias_gracia < 0:
                errores.append('Los días de gracia no pueden ser negativos.')
        except ValueError:
            errores.append('Los días de gracia deben ser un número entero.')
            dias_gracia = 0

        if errores:
            for error in errores:
                flash(error, 'danger')
            return redirect(url_for('configuracion_cobros'))

        config.tipo_recargo = TipoRecargo[tipo_raw]
        config.valor_recargo = valor
        config.dias_gracia = dias_gracia
        db.session.commit()

        flash('Configuración de recargos actualizada correctamente.', 'success')
        return redirect(url_for('configuracion_cobros'))

    return render_template('configuracion_cobros.html', config=config, tipos_recargo=list(TipoRecargo))


if __name__ == '__main__':
    # NOTA: ya NO se llama db.create_all() aquí. El esquema de la base de
    # datos lo administra exclusivamente Flask-Migrate (flask db upgrade).
    # Tener ambos mecanismos activos a la vez causaba estados inconsistentes
    # entre lo que create_all() creaba y lo que las migraciones esperaban.
    app.run(debug=True)
