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
import os
import re
from datetime import datetime
from functools import wraps

from flask import Flask, render_template, request, flash, redirect, url_for, abort, session
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from sqlalchemy import UniqueConstraint, CheckConstraint, or_
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from config import config_by_name

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address)


# ---------------------------------------------------------------------------
# ENUMS
# ---------------------------------------------------------------------------

class EstatusAlumno(enum.Enum):
    """
    Estatus del ciclo de vida del alumno dentro del sistema.
    'PENDIENTE' es el estatus inicial obligatorio al llegar desde el
    Módulo de Auto-registro Público (Módulo 1).
    """
    PENDIENTE = 'Pendiente de Validación'
    ACTIVO = 'Activo'
    BAJA_TEMPORAL = 'Baja Temporal'
    BAJA_DEFINITIVA = 'Baja Definitiva'
    EGRESADO = 'Egresado'


class TurnoAlumno(enum.Enum):
    MATUTINO = 'Matutino'
    VESPERTINO = 'Vespertino'
    MIXTO = 'Mixto'


class ModalidadEstudio(enum.Enum):
    ESCOLARIZADO = 'Escolarizado'
    SEMIESCOLARIZADO = 'Semiescolarizado'
    DISTANCIA = 'A Distancia'


class RolUsuario(enum.Enum):
    """
    Solo existen 2 roles humanos en el sistema; los docentes NO tienen rol
    (regla de negocio: cero acceso a maestros).

      - DIRECTIVO: control total. Puede modificar y borrar cualquier cosa,
        y es el ÚNICO que puede crear/gestionar cuentas de Administrativos.
      - ADMINISTRATIVO (Control Escolar): puede consultar y actualizar
        (registrar alumnos, subir documentos, capturar boletas), pero NO
        puede borrar documentos ni gestionar usuarios.
    """
    DIRECTIVO = 'Dirección / Administrador General'
    ADMINISTRATIVO = 'Administrativo (Control Escolar)'


# ---------------------------------------------------------------------------
# USUARIOS DEL SISTEMA (login)
# ---------------------------------------------------------------------------

class Usuario(UserMixin, db.Model):
    """
    Cuenta de acceso al sistema. UserMixin le da a Flask-Login las
    propiedades que necesita (is_authenticated, is_active, get_id, etc.).
    La contraseña NUNCA se guarda en texto plano: solo su hash.
    """
    __tablename__ = 'usuarios'

    id = db.Column(db.Integer, primary_key=True)
    nombre_completo = db.Column(db.String(150), nullable=False)
    username = db.Column(db.String(50), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    rol = db.Column(db.Enum(RolUsuario), nullable=False, default=RolUsuario.ADMINISTRATIVO)
    activo = db.Column(db.Boolean, default=True, nullable=False)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    ultimo_acceso = db.Column(db.DateTime, nullable=True)

    def set_password(self, password_plano: str) -> None:
        # pbkdf2:sha256 (default de Werkzeug): estándar robusto y ampliamente auditado.
        self.password_hash = generate_password_hash(password_plano)

    def check_password(self, password_plano: str) -> bool:
        return check_password_hash(self.password_hash, password_plano)

    def es_directivo(self) -> bool:
        return self.rol == RolUsuario.DIRECTIVO

    @property
    def is_active(self):
        # Sobreescribe el default de UserMixin (que siempre es True):
        # una cuenta desactivada por Dirección no puede iniciar sesión.
        return self.activo

    def __repr__(self):
        return f'<Usuario {self.username} ({self.rol.name})>'


# ---------------------------------------------------------------------------
# MODELOS
# ---------------------------------------------------------------------------

class PlanEstudio(db.Model):
    """
    Representa un Plan de Estudios oficial (ej. "Licenciatura en Enfermería
    Generación 2024"). Es el contenedor "blindado" de materias: nada se
    agrega aquí desde la captura de calificaciones, solo desde administración
    del plan.
    """
    __tablename__ = 'planes_estudio'

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(150), nullable=False)
    clave_carrera = db.Column(db.String(10), nullable=False)  # Ej: "LEN" -> usado para folio de matrícula
    anio_generacion = db.Column(db.Integer, nullable=False)
    duracion_anios = db.Column(db.Integer, nullable=True)  # Ej. 3 (para mostrar en la Ficha de Inscripción)
    activo = db.Column(db.Boolean, default=True, nullable=False)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

    # Relaciones
    materias = db.relationship(
        'Materia',
        backref='plan',
        lazy=True,
        cascade='all, delete-orphan'
    )
    alumnos = db.relationship(
        'Alumno',
        backref='plan',
        lazy=True
    )

    __table_args__ = (
        UniqueConstraint('clave_carrera', 'anio_generacion', name='uq_plan_clave_anio'),
    )

    def __repr__(self):
        return f'<PlanEstudio {self.clave_carrera}-{self.anio_generacion}: {self.nombre}>'


class Materia(db.Model):
    """
    Materia perteneciente OBLIGATORIAMENTE a un PlanEstudio.
    No puede existir una Materia "suelta": id_plan_fk es NOT NULL.
    Esto es lo que garantiza el "Escudo" del Plan de Estudios: la
    aplicación solo debe permitir seleccionar materias filtradas por
    Materia.query.filter_by(id_plan_fk=alumno.id_plan_fk).
    """
    __tablename__ = 'materias'

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(150), nullable=False)
    clave = db.Column(db.String(20), nullable=True)  # Clave oficial de la materia, opcional
    cuatrimestre = db.Column(db.Integer, nullable=False)
    creditos = db.Column(db.Float, nullable=True)

    id_plan_fk = db.Column(
        db.Integer,
        db.ForeignKey('planes_estudio.id'),
        nullable=False
    )

    # Relaciones
    calificaciones = db.relationship(
        'Calificacion',
        backref='materia',
        lazy=True
    )

    __table_args__ = (
        CheckConstraint('cuatrimestre > 0', name='ck_materia_cuatrimestre_positivo'),
        UniqueConstraint('id_plan_fk', 'nombre', 'cuatrimestre', name='uq_materia_por_plan'),
    )

    def __repr__(self):
        return f'<Materia {self.nombre} (Cuatri {self.cuatrimestre}) - Plan {self.id_plan_fk}>'


class Alumno(db.Model):
    """
    Entidad central del sistema. La matrícula (matricula_id) es la Primary
    Key y funciona como identificador único de negocio (no un id numérico
    autoincremental interno), ya que es el dato con el que Control Escolar
    y el propio alumno identifican su expediente.
    """
    __tablename__ = 'alumnos'

    matricula_id = db.Column(db.String(20), primary_key=True)  # Ej: LEN2024-00015
    nombre_completo = db.Column(db.String(200), nullable=False)
    curp = db.Column(db.String(18), unique=True, nullable=False, index=True)
    fecha_nacimiento = db.Column(db.Date, nullable=False)
    fecha_certificado_prepa = db.Column(db.Date, nullable=False)

    id_plan_fk = db.Column(
        db.Integer,
        db.ForeignKey('planes_estudio.id'),
        nullable=False
    )

    estatus = db.Column(
        db.Enum(EstatusAlumno),
        default=EstatusAlumno.PENDIENTE,
        nullable=False
    )

    grupo_actual = db.Column(db.String(20), nullable=True)  # Informativo; NUNCA usado como FK de calificaciones
    correo = db.Column(db.String(150), nullable=True)
    telefono = db.Column(db.String(20), nullable=True)  # Teléfono fijo
    telefono_movil = db.Column(db.String(20), nullable=True)

    # --- Datos personales adicionales (para la Ficha de Inscripción impresa) ---
    sexo = db.Column(db.String(20), nullable=True)  # Femenino / Masculino / Otro
    numero_identificacion = db.Column(db.String(30), nullable=True)  # Nº de INE u otra identificación oficial
    estado_civil = db.Column(db.String(30), nullable=True)
    nacionalidad = db.Column(db.String(50), nullable=True, default='Mexicana')
    tipo_sangre = db.Column(db.String(5), nullable=True)  # Ej. "O+", "A-"

    # --- Domicilio ---
    domicilio_calle_numero = db.Column(db.String(200), nullable=True)
    domicilio_ciudad = db.Column(db.String(100), nullable=True)
    domicilio_cp = db.Column(db.String(10), nullable=True)
    domicilio_estado = db.Column(db.String(100), nullable=True)

    # --- Contacto de emergencia (persona distinta al tutor legal) ---
    contacto_emergencia_nombre = db.Column(db.String(150), nullable=True)
    contacto_emergencia_telefono = db.Column(db.String(20), nullable=True)
    contacto_emergencia_parentesco = db.Column(db.String(50), nullable=True)  # Ej. "Hermana", "Madre"

    como_se_entero = db.Column(db.String(200), nullable=True)  # Ej. "Por una amiga", "Redes sociales"

    turno = db.Column(db.Enum(TurnoAlumno), nullable=True)
    modalidad = db.Column(db.Enum(ModalidadEstudio), nullable=True)

    # --- Datos ampliados del expediente (Módulo de Documentos) ---
    escuela_prepa = db.Column(db.String(200), nullable=True)
    alergias_condiciones = db.Column(db.Text, nullable=True)
    telefono_tutor = db.Column(db.String(20), nullable=True)

    # --- Seguimiento administrativo y académico (Control Escolar) ---
    # cuatrimestre_actual: en qué cuatrimestre inició/va el alumno. Por defecto 1
    # (nuevo ingreso), pero se puede ajustar en casos de revalidación/equivalencia
    # donde el alumno entra directo a un cuatrimestre avanzado.
    cuatrimestre_actual = db.Column(db.Integer, nullable=False, default=1)
    documentacion_pendiente = db.Column(db.Text, nullable=True)  # Ej. "Falta acta de nacimiento"
    materias_adeudadas = db.Column(db.Text, nullable=True)  # Nota manual; el cálculo automático llega con el Historial
    faltas_administrativas = db.Column(db.Text, nullable=True)  # Ej. "2 faltas por inasistencia a junta"

    fecha_registro = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_validacion = db.Column(db.DateTime, nullable=True)  # Se llena cuando admin aprueba el pre-registro

    # Relaciones
    calificaciones = db.relationship(
        'Calificacion',
        backref='alumno',
        lazy=True,
        cascade='all, delete-orphan'
    )
    documentos = db.relationship(
        'DocumentoAlumno',
        backref='alumno',
        lazy=True,
        cascade='all, delete-orphan'
    )

    def __repr__(self):
        return f'<Alumno {self.matricula_id} - {self.nombre_completo}>'

    def edad(self) -> int:
        """Calcula la edad actual del alumno a partir de su fecha de nacimiento."""
        hoy = datetime.utcnow().date()
        anios = hoy.year - self.fecha_nacimiento.year
        # Ajusta si aún no ha cumplido años este año
        if (hoy.month, hoy.day) < (self.fecha_nacimiento.month, self.fecha_nacimiento.day):
            anios -= 1
        return anios

    def materia_pertenece_a_su_plan(self, materia: 'Materia') -> bool:
        """
        Método de apoyo para el 'Escudo' del Plan de Estudios.
        Se usará en el Módulo de Captura de Calificaciones (Paso 4) para
        rechazar cualquier intento de registrar una calificación de una
        materia que no corresponda al plan asignado a este alumno.
        """
        return materia.id_plan_fk == self.id_plan_fk

    def promedio_general(self) -> float:
        """Calcula el promedio general con las calificaciones ya capturadas."""
        if not self.calificaciones:
            return 0.0
        suma = sum(c.calificacion_final for c in self.calificaciones)
        return round(suma / len(self.calificaciones), 2)


class TipoDocumento(enum.Enum):
    """Tipos de documentos digitalizados que se pueden anexar al expediente."""
    COMPROBANTE_DOMICILIO = 'Comprobante de Domicilio'
    INE = 'INE / Identificación Oficial'
    CURP_DOC = 'CURP (documento)'
    ACTA_NACIMIENTO = 'Acta de Nacimiento'
    CERTIFICADO_PREPA = 'Certificado de Preparatoria'
    FOTOGRAFIA = 'Fotografía'
    OTRO = 'Otro'


class DocumentoAlumno(db.Model):
    """
    Archivo digitalizado anexado al expediente de un Alumno
    (comprobante de domicilio, INE, CURP, etc.). El archivo físico se
    guarda en disco bajo UPLOAD_FOLDER/<matricula>/ y aquí solo se guarda
    la referencia/metadatos, para no inflar la base de datos con binarios.
    """
    __tablename__ = 'documentos_alumno'

    id = db.Column(db.Integer, primary_key=True)

    matricula_fk = db.Column(
        db.String(20),
        db.ForeignKey('alumnos.matricula_id'),
        nullable=False
    )

    tipo_documento = db.Column(db.Enum(TipoDocumento), nullable=False)
    nombre_archivo_original = db.Column(db.String(255), nullable=False)
    ruta_archivo = db.Column(db.String(500), nullable=False)  # Ruta relativa dentro de UPLOAD_FOLDER
    fecha_subida = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<DocumentoAlumno {self.matricula_fk} - {self.tipo_documento.value}>'


class Calificacion(db.Model):
    """
    Calificación final de un Alumno en una Materia, para un periodo escolar
    determinado. Se vincula SIEMPRE a la matrícula del alumno (no al grupo),
    cumpliendo la regla de negocio "Centrado en el Alumno".

    La restricción única (matricula_fk, id_materia_fk, periodo_escolar)
    evita capturas duplicadas accidentales de la misma acta.
    """
    __tablename__ = 'calificaciones'

    id = db.Column(db.Integer, primary_key=True)

    matricula_fk = db.Column(
        db.String(20),
        db.ForeignKey('alumnos.matricula_id'),
        nullable=False
    )
    id_materia_fk = db.Column(
        db.Integer,
        db.ForeignKey('materias.id'),
        nullable=False
    )

    calificacion_final = db.Column(db.Float, nullable=False)
    periodo_escolar = db.Column(db.String(20), nullable=False)  # Ej: "2025-B", "Ene-Abr 2025"

    fecha_captura = db.Column(db.DateTime, default=datetime.utcnow)
    capturado_por = db.Column(db.String(100), nullable=True)  # Usuario admin que capturó (auditoría)
    numero_acta = db.Column(db.String(50), nullable=True)  # Referencia al acta física firmada

    __table_args__ = (
        UniqueConstraint(
            'matricula_fk', 'id_materia_fk', 'periodo_escolar',
            name='uq_calificacion_alumno_materia_periodo'
        ),
        CheckConstraint(
            'calificacion_final >= 0 AND calificacion_final <= 10',
            name='ck_calificacion_rango_valido'
        ),
    )

    def __repr__(self):
        return f'<Calificacion {self.matricula_fk} / Materia {self.id_materia_fk}: {self.calificacion_final}>'


class HistorialEstatus(db.Model):
    """
    Auditoría de cada cambio de estatus de un alumno (ej. Pendiente -> Activo,
    Activo -> Egresado). Nunca se sobreescribe ni se borra: es un registro
    histórico de "quién cambió qué y cuándo".
    """
    __tablename__ = 'historial_estatus'

    id = db.Column(db.Integer, primary_key=True)

    matricula_fk = db.Column(
        db.String(20),
        db.ForeignKey('alumnos.matricula_id'),
        nullable=False
    )
    usuario_fk = db.Column(
        db.Integer,
        db.ForeignKey('usuarios.id'),
        nullable=True  # Nullable por si el usuario se llega a borrar en el futuro
    )

    estatus_anterior = db.Column(db.Enum(EstatusAlumno), nullable=True)  # Nulo en el primer registro
    estatus_nuevo = db.Column(db.Enum(EstatusAlumno), nullable=False)
    comentario = db.Column(db.String(255), nullable=True)  # Ej. "Egresado forzado sin todas las materias"
    fecha = db.Column(db.DateTime, default=datetime.utcnow)

    alumno = db.relationship('Alumno', backref=db.backref('historial_estatus', lazy=True, order_by='HistorialEstatus.fecha.desc()'))
    usuario = db.relationship('Usuario')

    def __repr__(self):
        return f'<HistorialEstatus {self.matricula_fk}: {self.estatus_anterior} -> {self.estatus_nuevo}>'


# ---------------------------------------------------------------------------
# APPLICATION FACTORY
# ---------------------------------------------------------------------------

def create_app(config_name='development'):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_by_name[config_name])

    db.init_app(app)
    migrate.init_app(app, db)

    login_manager.init_app(app)
    login_manager.login_view = 'login'
    login_manager.login_message = 'Debes iniciar sesión para acceder a esta página.'
    login_manager.login_message_category = 'warning'

    csrf.init_app(app)
    limiter.init_app(app)

    # Asegura que exista la carpeta física donde se guardan los documentos
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

    # Aquí se registrarán los Blueprints en pasos posteriores:
    # from routes.registro import registro_bp
    # from routes.admin import admin_bp
    # app.register_blueprint(registro_bp)
    # app.register_blueprint(admin_bp)

    return app


app = create_app()


@app.errorhandler(429)
def limite_intentos_excedido(error):
    """
    Se dispara cuando Flask-Limiter bloquea una IP por exceder el límite de
    intentos de login. En vez del 429 genérico de Werkzeug, mostramos un
    mensaje claro y regresamos a la pantalla de login.
    """
    flash('Demasiados intentos de inicio de sesión. Por seguridad, espera un minuto e inténtalo de nuevo.', 'danger')
    return redirect(url_for('login'))


@login_manager.user_loader
def load_user(user_id):
    return Usuario.query.get(int(user_id))


def rol_requerido(*roles_permitidos):
    """
    Decorador para restringir una ruta a ciertos roles (ej. solo DIRECTIVO).
    Siempre exige login primero (@login_required incluido). Uso:

        @app.route('/algo-solo-de-dirección')
        @rol_requerido('DIRECTIVO')
        def algo():
            ...
    """
    def decorador(func):
        @wraps(func)
        @login_required
        def envoltura(*args, **kwargs):
            if current_user.rol.name not in roles_permitidos:
                flash('No tienes permisos para realizar esta acción.', 'danger')
                return redirect(url_for('index'))
            return func(*args, **kwargs)
        return envoltura
    return decorador


# ---------------------------------------------------------------------------
# AUTENTICACIÓN
# ---------------------------------------------------------------------------

@app.route('/login', methods=['GET', 'POST'])
@limiter.limit('5 per minute', methods=['POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        recordar = request.form.get('recordar') == 'on'

        usuario = Usuario.query.filter_by(username=username).first()

        # Mensaje genérico a propósito: no revelamos si falló el usuario
        # o la contraseña, para no facilitar enumeración de cuentas.
        if usuario and usuario.activo and usuario.check_password(password):
            session.permanent = True  # Activa PERMANENT_SESSION_LIFETIME (expira tras 8h de inactividad)
            login_user(usuario, remember=recordar)
            usuario.ultimo_acceso = datetime.utcnow()
            db.session.commit()

            flash(f'Bienvenido, {usuario.nombre_completo}.', 'success')
            siguiente = request.args.get('next')
            return redirect(siguiente or url_for('index'))

        flash('Usuario o contraseña incorrectos.', 'danger')

    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Sesión cerrada correctamente.', 'success')
    return redirect(url_for('login'))


@app.route('/perfil', methods=['GET', 'POST'])
@login_required
def perfil():
    """Cada usuario (Directivo o Administrativo) edita SU propia cuenta aquí."""
    if request.method == 'POST':
        nombre_completo = request.form.get('nombre_completo', '').strip()
        if nombre_completo:
            current_user.nombre_completo = nombre_completo

        password_actual = request.form.get('password_actual', '')
        password_nueva = request.form.get('password_nueva', '')
        password_confirmar = request.form.get('password_confirmar', '')

        if password_nueva or password_confirmar or password_actual:
            if not current_user.check_password(password_actual):
                flash('Tu contraseña actual no es correcta; no se cambió nada.', 'danger')
                return redirect(url_for('perfil'))
            if len(password_nueva) < 8:
                flash('La nueva contraseña debe tener al menos 8 caracteres.', 'danger')
                return redirect(url_for('perfil'))
            if password_nueva != password_confirmar:
                flash('La confirmación no coincide con la nueva contraseña.', 'danger')
                return redirect(url_for('perfil'))
            current_user.set_password(password_nueva)
            flash('Contraseña actualizada correctamente.', 'success')

        db.session.commit()
        flash('Perfil actualizado.', 'success')
        return redirect(url_for('perfil'))

    return render_template('perfil.html')


# ---------------------------------------------------------------------------
# GESTIÓN DE ADMINISTRATIVOS (solo DIRECTIVO)
# ---------------------------------------------------------------------------

@app.route('/usuarios')
@rol_requerido('DIRECTIVO')
def usuarios():
    lista = Usuario.query.order_by(Usuario.rol.asc(), Usuario.nombre_completo.asc()).all()
    return render_template('usuarios.html', usuarios=lista)


@app.route('/usuarios/nuevo', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO')
def nuevo_usuario():
    if request.method == 'POST':
        nombre_completo = request.form.get('nombre_completo', '').strip()
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        confirmar = request.form.get('confirmar_password', '')
        rol_raw = request.form.get('rol', 'ADMINISTRATIVO')

        errores = []
        if len(nombre_completo) < 3:
            errores.append('Ingresa el nombre completo del usuario.')
        if len(username) < 4 or not re.match(r'^[a-z0-9_.]+$', username):
            errores.append('El usuario debe tener al menos 4 caracteres (letras, números, "." o "_").')
        elif Usuario.query.filter_by(username=username).first():
            errores.append(f'El usuario "{username}" ya existe.')
        if len(password) < 8:
            errores.append('La contraseña debe tener al menos 8 caracteres.')
        elif password != confirmar:
            errores.append('Las contraseñas no coinciden.')
        if rol_raw not in RolUsuario.__members__:
            errores.append('Selecciona un rol válido.')

        if errores:
            for error in errores:
                flash(error, 'danger')
            return render_template('nuevo_usuario.html'), 400

        nuevo = Usuario(
            nombre_completo=nombre_completo,
            username=username,
            rol=RolUsuario[rol_raw],
            activo=True
        )
        nuevo.set_password(password)

        try:
            db.session.add(nuevo)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash('Ocurrió un error al crear el usuario. Intenta de nuevo.', 'danger')
            return render_template('nuevo_usuario.html'), 400

        flash(f'Usuario "{username}" ({nuevo.rol.value}) creado correctamente.', 'success')
        return redirect(url_for('usuarios'))

    return render_template('nuevo_usuario.html')


@app.route('/usuarios/<int:user_id>/toggle', methods=['POST'])
@rol_requerido('DIRECTIVO')
def toggle_usuario(user_id):
    """Activa/desactiva una cuenta sin borrarla (mejor que eliminarla: conserva auditoría)."""
    usuario = Usuario.query.get_or_404(user_id)

    if usuario.id == current_user.id:
        flash('No puedes desactivar tu propia cuenta.', 'danger')
        return redirect(url_for('usuarios'))

    usuario.activo = not usuario.activo
    db.session.commit()

    estado = 'activada' if usuario.activo else 'desactivada'
    flash(f'La cuenta de "{usuario.username}" fue {estado}.', 'success')
    return redirect(url_for('usuarios'))


# ---------------------------------------------------------------------------
# MÓDULO DE BÚSQUEDA UNIVERSAL
# Pantalla principal de uso exclusivo de Control Escolar.
# ---------------------------------------------------------------------------

def calcular_estadisticas_alumnos():
    """Números clave para el panel del buscador (pantalla de inicio)."""
    return {
        'total': Alumno.query.count(),
        'pendientes': Alumno.query.filter_by(estatus=EstatusAlumno.PENDIENTE).count(),
        'activos': Alumno.query.filter_by(estatus=EstatusAlumno.ACTIVO).count(),
        'documentacion_incompleta': Alumno.query.filter(Alumno.documentacion_pendiente.isnot(None)).count(),
        'faltas': Alumno.query.filter(Alumno.faltas_administrativas.isnot(None)).count(),
    }


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
        'recientes': ('Últimos 10 Alumnos Registrados', None),
    }

    filtro = request.args.get('filtro')
    resultados = None
    titulo_filtro = None

    if filtro in filtros_disponibles:
        titulo_filtro, condicion = filtros_disponibles[filtro]
        if filtro == 'recientes':
            resultados = Alumno.query.order_by(Alumno.fecha_registro.desc()).limit(10).all()
        else:
            resultados = Alumno.query.filter(condicion).order_by(Alumno.nombre_completo.asc()).all()

    return render_template(
        'buscador.html',
        estadisticas=estadisticas,
        resultados=resultados,
        titulo_filtro=titulo_filtro
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

    resultados = Alumno.query.filter(
        or_(
            Alumno.matricula_id == termino,
            Alumno.nombre_completo.ilike(f'%{termino}%')
        )
    ).order_by(Alumno.nombre_completo.asc()).all()

    if not resultados:
        flash(f'No se encontraron alumnos que coincidan con "{termino}".', 'danger')
        return redirect(url_for('index'))

    return render_template('buscador.html', resultados=resultados, termino=termino)


# ---------------------------------------------------------------------------
# MÓDULO DE AUTO-REGISTRO PÚBLICO
# ---------------------------------------------------------------------------

def generar_matricula(plan: 'PlanEstudio') -> str:
    """
    Genera la matrícula siguiente para un plan dado, con el formato:
        <CLAVE_CARRERA><AÑO_ACTUAL>-<CONSECUTIVO 5 dígitos>
    Ej: LEN2026-00001, LEN2026-00002, ...
    El consecutivo se calcula buscando la última matrícula ya usada
    con ese mismo prefijo (carrera + año), NO el total de alumnos,
    para que nunca se reutilice un número aunque se den de baja alumnos.
    """
    anio_actual = datetime.utcnow().year
    prefijo = f"{plan.clave_carrera}{anio_actual}-"

    ultimo = (
        Alumno.query
        .filter(Alumno.matricula_id.like(f"{prefijo}%"))
        .order_by(Alumno.matricula_id.desc())
        .first()
    )

    if ultimo:
        ultimo_num = int(ultimo.matricula_id.split('-')[-1])
        siguiente = ultimo_num + 1
    else:
        siguiente = 1

    return f"{prefijo}{siguiente:05d}"


@app.route('/registro', methods=['GET', 'POST'])
def registro():
    planes = PlanEstudio.query.filter_by(activo=True).order_by(PlanEstudio.nombre.asc()).all()

    if request.method == 'GET':
        return render_template('registro.html', planes=planes)

    # --- POST: procesar el formulario ---
    nombre_completo = request.form.get('nombre_completo', '').strip()
    curp = request.form.get('curp', '').strip().upper()
    fecha_nacimiento_raw = request.form.get('fecha_nacimiento', '')
    fecha_certificado_raw = request.form.get('fecha_certificado_prepa', '')
    id_plan_raw = request.form.get('id_plan_fk', '')
    correo = request.form.get('correo', '').strip() or None
    telefono = request.form.get('telefono', '').strip() or None
    telefono_movil = request.form.get('telefono_movil', '').strip() or None

    sexo = request.form.get('sexo', '').strip() or None
    numero_identificacion = request.form.get('numero_identificacion', '').strip() or None
    estado_civil = request.form.get('estado_civil', '').strip() or None
    nacionalidad = request.form.get('nacionalidad', '').strip() or 'Mexicana'
    tipo_sangre = request.form.get('tipo_sangre', '').strip() or None

    domicilio_calle_numero = request.form.get('domicilio_calle_numero', '').strip() or None
    domicilio_ciudad = request.form.get('domicilio_ciudad', '').strip() or None
    domicilio_cp = request.form.get('domicilio_cp', '').strip() or None
    domicilio_estado = request.form.get('domicilio_estado', '').strip() or None

    contacto_emergencia_nombre = request.form.get('contacto_emergencia_nombre', '').strip() or None
    contacto_emergencia_telefono = request.form.get('contacto_emergencia_telefono', '').strip() or None
    contacto_emergencia_parentesco = request.form.get('contacto_emergencia_parentesco', '').strip() or None

    como_se_entero = request.form.get('como_se_entero', '').strip() or None

    turno_raw = request.form.get('turno', '')
    modalidad_raw = request.form.get('modalidad', '')

    errores = []

    if len(nombre_completo) < 5:
        errores.append('Ingresa tu nombre completo correctamente.')

    if not re.match(r'^[A-Z0-9]{18}$', curp):
        errores.append('La CURP debe tener exactamente 18 caracteres alfanuméricos.')
    elif Alumno.query.filter_by(curp=curp).first():
        errores.append(f'Ya existe un alumno registrado con la CURP "{curp}".')

    if not sexo:
        errores.append('Selecciona tu sexo.')

    if not domicilio_calle_numero or not domicilio_ciudad or not domicilio_cp or not domicilio_estado:
        errores.append('Completa todos los campos de tu domicilio.')

    if not contacto_emergencia_nombre or not contacto_emergencia_telefono:
        errores.append('Indica el nombre y teléfono de tu contacto de emergencia.')

    turno = None
    if turno_raw not in TurnoAlumno.__members__:
        errores.append('Selecciona un turno válido.')
    else:
        turno = TurnoAlumno[turno_raw]

    modalidad = None
    if modalidad_raw not in ModalidadEstudio.__members__:
        errores.append('Selecciona una modalidad válida.')
    else:
        modalidad = ModalidadEstudio[modalidad_raw]

    fecha_nacimiento = None
    try:
        fecha_nacimiento = datetime.strptime(fecha_nacimiento_raw, '%Y-%m-%d').date()
    except ValueError:
        errores.append('La fecha de nacimiento no es válida.')

    fecha_certificado_prepa = None
    try:
        fecha_certificado_prepa = datetime.strptime(fecha_certificado_raw, '%Y-%m-%d').date()
    except ValueError:
        errores.append('La fecha del certificado de preparatoria no es válida.')

    plan = None
    if not id_plan_raw:
        errores.append('Debes seleccionar tu carrera / plan de estudios.')
    else:
        try:
            plan = PlanEstudio.query.get(int(id_plan_raw))
        except (ValueError, TypeError):
            plan = None
        if not plan or not plan.activo:
            errores.append('El plan de estudios seleccionado no es válido.')

    if errores:
        for error in errores:
            flash(error, 'danger')
        # Reenviamos el formulario con los planes para no perder el <select>
        return render_template('registro.html', planes=planes), 400

    matricula = generar_matricula(plan)

    nuevo_alumno = Alumno(
        matricula_id=matricula,
        nombre_completo=nombre_completo,
        curp=curp,
        fecha_nacimiento=fecha_nacimiento,
        fecha_certificado_prepa=fecha_certificado_prepa,
        id_plan_fk=plan.id,
        estatus=EstatusAlumno.PENDIENTE,
        correo=correo,
        telefono=telefono,
        telefono_movil=telefono_movil,
        sexo=sexo,
        numero_identificacion=numero_identificacion,
        estado_civil=estado_civil,
        nacionalidad=nacionalidad,
        tipo_sangre=tipo_sangre,
        domicilio_calle_numero=domicilio_calle_numero,
        domicilio_ciudad=domicilio_ciudad,
        domicilio_cp=domicilio_cp,
        domicilio_estado=domicilio_estado,
        contacto_emergencia_nombre=contacto_emergencia_nombre,
        contacto_emergencia_telefono=contacto_emergencia_telefono,
        contacto_emergencia_parentesco=contacto_emergencia_parentesco,
        como_se_entero=como_se_entero,
        turno=turno,
        modalidad=modalidad,
    )

    try:
        db.session.add(nuevo_alumno)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash('Ocurrió un error al guardar tu registro. Verifica tus datos e intenta de nuevo.', 'danger')
        return render_template('registro.html', planes=planes), 400

    flash(
        f'¡Registro exitoso! Tu matrícula es <strong>{matricula}</strong>. '
        'Tu solicitud quedó en estatus "Pendiente de Validación" y será revisada por Control Escolar.',
        'success'
    )
    return redirect(url_for('registro'))


# ---------------------------------------------------------------------------
# MÓDULO DE DOCUMENTOS DEL EXPEDIENTE
# Permite completar los datos que NO se piden en el auto-registro público
# (escuela de prepa, alergias, teléfono del tutor) y subir los archivos
# digitalizados: comprobante de domicilio, INE y CURP.
# ---------------------------------------------------------------------------

def extension_permitida(nombre_archivo: str) -> bool:
    return (
        '.' in nombre_archivo
        and nombre_archivo.rsplit('.', 1)[1].lower() in app.config['EXTENSIONES_PERMITIDAS']
    )


# Mapeo de <name> del <input type="file"> -> TipoDocumento correspondiente
CAMPOS_DOCUMENTOS = {
    'archivo_domicilio': TipoDocumento.COMPROBANTE_DOMICILIO,
    'archivo_ine': TipoDocumento.INE,
    'archivo_curp': TipoDocumento.CURP_DOC,
    'archivo_acta': TipoDocumento.ACTA_NACIMIENTO,
    'archivo_certificado': TipoDocumento.CERTIFICADO_PREPA,
    'archivo_foto': TipoDocumento.FOTOGRAFIA,
}


@app.route('/alumno/<matricula>/documentos', methods=['GET', 'POST'])
@login_required
def documentos(matricula):
    alumno = Alumno.query.get_or_404(matricula)

    if request.method == 'POST':
        # --- 1. Actualizar datos complementarios del expediente ---
        alumno.escuela_prepa = request.form.get('escuela_prepa', '').strip() or alumno.escuela_prepa
        alumno.alergias_condiciones = request.form.get('alergias_condiciones', '').strip() or None
        alumno.telefono = request.form.get('telefono', '').strip() or alumno.telefono
        alumno.telefono_tutor = request.form.get('telefono_tutor', '').strip() or None

        # --- 1b. Seguimiento administrativo y académico ---
        cuatrimestre_raw = request.form.get('cuatrimestre_actual', '').strip()
        if cuatrimestre_raw.isdigit():
            alumno.cuatrimestre_actual = int(cuatrimestre_raw)
        alumno.documentacion_pendiente = request.form.get('documentacion_pendiente', '').strip() or None
        alumno.materias_adeudadas = request.form.get('materias_adeudadas', '').strip() or None
        alumno.faltas_administrativas = request.form.get('faltas_administrativas', '').strip() or None

        # --- 2. Procesar cada archivo subido (todos opcionales) ---
        carpeta_alumno = os.path.join(app.config['UPLOAD_FOLDER'], alumno.matricula_id)
        archivos_guardados = 0

        for campo_form, tipo_doc in CAMPOS_DOCUMENTOS.items():
            archivo = request.files.get(campo_form)

            if not archivo or archivo.filename == '':
                continue  # No se seleccionó archivo para este campo, se omite

            if not extension_permitida(archivo.filename):
                flash(
                    f'El archivo de "{tipo_doc.value}" tiene un formato no permitido '
                    f'(solo PDF, JPG o PNG).',
                    'danger'
                )
                continue

            os.makedirs(carpeta_alumno, exist_ok=True)
            nombre_seguro = secure_filename(archivo.filename)
            nombre_final = f'{tipo_doc.name}_{int(datetime.utcnow().timestamp())}_{nombre_seguro}'
            ruta_absoluta = os.path.join(carpeta_alumno, nombre_final)
            archivo.save(ruta_absoluta)

            documento = DocumentoAlumno(
                matricula_fk=alumno.matricula_id,
                tipo_documento=tipo_doc,
                nombre_archivo_original=archivo.filename,
                # OJO: aquí SIEMPRE forward-slash, aunque sea Windows, porque esta
                # ruta se usa para construir URLs (Flask sirve /static/... con '/').
                # os.path.join() usaría '\' en Windows y rompería el link (error 404).
                ruta_archivo=f'{alumno.matricula_id}/{nombre_final}',
            )
            db.session.add(documento)
            archivos_guardados += 1

        db.session.commit()

        if archivos_guardados:
            flash(f'Se guardaron {archivos_guardados} documento(s) y se actualizó la información.', 'success')
        else:
            flash('Se actualizó la información del expediente.', 'success')

        return redirect(url_for('documentos', matricula=matricula))

    documentos_alumno = (
        DocumentoAlumno.query
        .filter_by(matricula_fk=matricula)
        .order_by(DocumentoAlumno.fecha_subida.desc())
        .all()
    )
    return render_template(
        'documentos.html',
        alumno=alumno,
        documentos=documentos_alumno,
        max_cuatrimestres=app.config['CUATRIMESTRES_MAXIMOS']
    )


@app.route('/documento/<int:doc_id>/eliminar', methods=['POST'])
@rol_requerido('DIRECTIVO')
def eliminar_documento(doc_id):
    """
    Elimina un documento subido por equivocación: borra el archivo físico
    del disco y su registro en la base de datos, y regresa al expediente
    del mismo alumno.
    """
    documento = DocumentoAlumno.query.get_or_404(doc_id)
    matricula = documento.matricula_fk
    tipo_valor = documento.tipo_documento.value

    ruta_absoluta = os.path.join(
        app.config['UPLOAD_FOLDER'],
        documento.ruta_archivo.replace('/', os.sep)
    )

    try:
        if os.path.exists(ruta_absoluta):
            os.remove(ruta_absoluta)
    except OSError:
        # Si el archivo físico ya no está en disco, no bloqueamos el borrado
        # del registro en BD; igual se lo informamos al usuario.
        flash('El archivo físico ya no se encontró en el servidor; se eliminó el registro.', 'warning')

    db.session.delete(documento)
    db.session.commit()

    flash(f'Se eliminó el documento "{tipo_valor}".', 'success')
    return redirect(url_for('documentos', matricula=matricula))


# ---------------------------------------------------------------------------
# PERFIL DEL ESTUDIANTE / EXPEDIENTE ACADÉMICO
# ---------------------------------------------------------------------------

@app.route('/alumno/<matricula>/expediente')
@login_required
def ver_expediente(matricula):
    alumno = Alumno.query.get_or_404(matricula)

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
        max_cuatrimestres=app.config['CUATRIMESTRES_MAXIMOS'],
        estatus_disponibles=list(EstatusAlumno),
        materias_no_aprobadas=materias_no_aprobadas,
        historial_estatus=alumno.historial_estatus
    )


@app.route('/alumno/<matricula>/ficha')
@login_required
def ficha_inscripcion(matricula):
    """
    Ficha de Inscripción imprimible (frente + reverso), a partir de los
    datos ya capturados en el registro público y el módulo de documentos.
    No requiere formulario propio: es una vista de solo lectura para
    imprimir/archivar, con checklist de documentos recibidos en el reverso.
    """
    alumno = Alumno.query.get_or_404(matricula)

    documentos_alumno = DocumentoAlumno.query.filter_by(matricula_fk=matricula).all()
    documentos_subidos = {doc.tipo_documento for doc in documentos_alumno}

    return render_template(
        'ficha_inscripcion.html',
        alumno=alumno,
        tipos_documento=list(TipoDocumento),
        documentos_subidos=documentos_subidos
    )


@app.route('/alumno/<matricula>/cambiar-estatus', methods=['POST'])
@login_required
def cambiar_estatus(matricula):
    """
    Cambia el estatus del alumno (ej. Pendiente -> Activo -> Egresado).
    Registra el cambio en HistorialEstatus para auditoría. Si se intenta
    marcar como EGRESADO y el alumno tiene materias sin aprobar, se pide
    un comentario justificando el motivo (advertencia, no bloqueo total).
    """
    alumno = Alumno.query.get_or_404(matricula)
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

    registro = HistorialEstatus(
        matricula_fk=alumno.matricula_id,
        usuario_fk=current_user.id,
        estatus_anterior=alumno.estatus,
        estatus_nuevo=nuevo_estatus,
        comentario=comentario,
    )
    alumno.estatus = nuevo_estatus

    if nuevo_estatus == EstatusAlumno.ACTIVO and not alumno.fecha_validacion:
        alumno.fecha_validacion = datetime.utcnow()

    db.session.add(registro)
    db.session.commit()

    flash(f'Estatus del alumno actualizado a "{nuevo_estatus.value}".', 'success')
    return redirect(url_for('ver_expediente', matricula=matricula))


# ---------------------------------------------------------------------------
# MÓDULO DE CAPTURA DE CALIFICACIONES (BOLETA)
# Solo Control Escolar captura, con base en actas físicas firmadas.
# El "Escudo" se aplica filtrando SIEMPRE por id_plan_fk del alumno.
# ---------------------------------------------------------------------------

@app.route('/alumno/<matricula>/boleta', methods=['GET', 'POST'])
@login_required
def boleta(matricula):
    alumno = Alumno.query.get_or_404(matricula)

    cuatrimestre_seleccionado = request.values.get('cuatrimestre', type=int)
    if not cuatrimestre_seleccionado:
        cuatrimestre_seleccionado = alumno.cuatrimestre_actual or 1

    # ESCUDO DEL PLAN DE ESTUDIOS: solo materias de ESTE plan y ESTE cuatrimestre.
    # Nunca se ofrece (ni se acepta) una materia fuera de esta consulta.
    materias = (
        Materia.query
        .filter_by(id_plan_fk=alumno.id_plan_fk, cuatrimestre=cuatrimestre_seleccionado)
        .order_by(Materia.nombre.asc())
        .all()
    )

    if request.method == 'POST':
        periodo_escolar = request.form.get('periodo_escolar', '').strip()
        numero_acta = request.form.get('numero_acta', '').strip() or None
        capturado_por = request.form.get('capturado_por', '').strip() or None

        if not periodo_escolar:
            flash('Indica el periodo escolar (ej. "2026-A") antes de guardar.', 'danger')
            return redirect(url_for('boleta', matricula=matricula, cuatrimestre=cuatrimestre_seleccionado))

        guardadas = 0
        for materia in materias:
            valor_raw = request.form.get(f'calificacion_{materia.id}', '').strip()

            if valor_raw == '':
                continue  # Casilla vacía = aún no se captura, se omite sin error

            try:
                valor = float(valor_raw)
            except ValueError:
                flash(f'La calificación de "{materia.nombre}" no es un número válido.', 'danger')
                continue

            if valor < 0 or valor > 10:
                flash(f'La calificación de "{materia.nombre}" debe estar entre 0 y 10.', 'danger')
                continue

            # Doble verificación del "Escudo" a nivel de objeto, por si acaso.
            if not alumno.materia_pertenece_a_su_plan(materia):
                flash('Se rechazó una materia que no pertenece al plan del alumno.', 'danger')
                continue

            existente = Calificacion.query.filter_by(
                matricula_fk=alumno.matricula_id,
                id_materia_fk=materia.id,
                periodo_escolar=periodo_escolar
            ).first()

            if existente:
                existente.calificacion_final = valor
                existente.numero_acta = numero_acta
                existente.capturado_por = capturado_por
            else:
                db.session.add(Calificacion(
                    matricula_fk=alumno.matricula_id,
                    id_materia_fk=materia.id,
                    calificacion_final=valor,
                    periodo_escolar=periodo_escolar,
                    numero_acta=numero_acta,
                    capturado_por=capturado_por,
                ))
            guardadas += 1

        db.session.commit()
        flash(f'Se guardaron {guardadas} calificación(es) del {cuatrimestre_seleccionado}° cuatrimestre.', 'success')
        return redirect(url_for('boleta', matricula=matricula, cuatrimestre=cuatrimestre_seleccionado))

    # Precargar calificaciones existentes de las materias de este cuatrimestre
    calificaciones_existentes = {
        c.id_materia_fk: c
        for c in alumno.calificaciones
        if c.materia.cuatrimestre == cuatrimestre_seleccionado
    }

    return render_template(
        'boleta.html',
        alumno=alumno,
        materias=materias,
        cuatrimestre_seleccionado=cuatrimestre_seleccionado,
        max_cuatrimestres=app.config['CUATRIMESTRES_MAXIMOS'],
        calificaciones_existentes=calificaciones_existentes,
    )


if __name__ == '__main__':
    with app.app_context():
        # Crea las tablas si no existen (solo desarrollo).
        # En producción se recomienda usar Flask-Migrate (ver Paso posterior).
        db.create_all()
    app.run(debug=True)
