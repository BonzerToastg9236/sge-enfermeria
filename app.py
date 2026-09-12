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

    from rutas.alumnos import alumnos_bp
    app.register_blueprint(alumnos_bp)

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
    return redirect(ruta_previa if ruta_previa.startswith('/') else url_for('alumnos.index'))


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





# ---------------------------------------------------------------------------
# PERFIL DEL ESTUDIANTE / EXPEDIENTE ACADÉMICO
# ---------------------------------------------------------------------------



if __name__ == '__main__':
    # NOTA: ya NO se llama db.create_all() aquí. El esquema de la base de
    # datos lo administra exclusivamente Flask-Migrate (flask db upgrade).
    # Tener ambos mecanismos activos a la vez causaba estados inconsistentes
    # entre lo que create_all() creaba y lo que las migraciones esperaban.
    app.run(debug=True)
