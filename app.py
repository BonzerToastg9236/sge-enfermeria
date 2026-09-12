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
from utilidades.paginacion import ALUMNOS_POR_PAGINA
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
from servicios.reportes import _calcular_reporte_cobros_del_dia
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

    from rutas.configuracion import configuracion_bp
    app.register_blueprint(configuracion_bp)

    from rutas.reportes import reportes_bp
    app.register_blueprint(reportes_bp)

    from rutas.cobros import cobros_bp
    app.register_blueprint(cobros_bp)

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


if __name__ == '__main__':
    # NOTA: ya NO se llama db.create_all() aquí. El esquema de la base de
    # datos lo administra exclusivamente Flask-Migrate (flask db upgrade).
    # Tener ambos mecanismos activos a la vez causaba estados inconsistentes
    # entre lo que create_all() creaba y lo que las migraciones esperaban.
    app.run(debug=True)
