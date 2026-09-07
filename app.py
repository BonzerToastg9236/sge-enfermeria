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


ZONA_HORARIA_DEFAULT = 'America/Mexico_City'


def ahora_utc():
    """
    Reemplazo de datetime.utcnow() (deprecado desde Python 3.12). Devuelve
    un datetime NAIVE en UTC -- igual que utcnow() devolvía -- para no
    cambiar cómo se comparan/guardan las fechas ya existentes en la BD
    (columnas DateTime sin timezone). datetime.now(timezone.utc) por sí solo
    devuelve un datetime AWARE, que no se puede comparar directamente con
    los naive que ya hay guardados -- por eso el .replace(tzinfo=None).
    SIGUE SIENDO CORRECTO PARA GUARDAR en la base de datos: todas las
    columnas DateTime se guardan en UTC a propósito. Para REGLAS DE
    NEGOCIO ("¿qué día es hoy para la institución?") usar hoy_local().
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _zona_horaria() -> ZoneInfo:
    """
    Zona horaria de la institución (México), configurable vía
    ZONA_HORARIA en config.py/entorno. No se cachea: se resuelve en cada
    llamada para que los tests puedan fijar una zona distinta sin reiniciar
    la app.
    """
    return ZoneInfo(app.config.get('ZONA_HORARIA', ZONA_HORARIA_DEFAULT))


def hoy_local(tz: ZoneInfo | None = None) -> date:
    """
    Fecha de HOY en hora local -- para REGLAS DE NEGOCIO (vencimientos,
    recargos, folios, "día" de un reporte de corte). NUNCA usar esto para
    guardar en la base de datos (eso sigue siendo ahora_utc()).
    Recibe tz opcional para poder probarse sin depender de la app real.
    """
    return datetime.now(tz or _zona_horaria()).date()


def a_local(dt: datetime | None, tz: ZoneInfo | None = None) -> datetime | None:
    """
    Convierte un datetime guardado (naive, en UTC) a hora local naive --
    para MOSTRAR en plantillas. Si ya llega con tzinfo, se respeta tal cual
    en vez de asumir UTC por encima.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz or _zona_horaria()).replace(tzinfo=None)


def rango_utc_del_dia(fecha_local: date, tz: ZoneInfo | None = None) -> tuple[datetime, datetime]:
    """
    Convierte un día calendario LOCAL completo (00:00:00 a 23:59:59.999999)
    a su rango equivalente en UTC naive -- para filtrar columnas DateTime
    (guardadas en UTC) por "día" tal como lo vive la institución, no como
    lo vive el servidor. Ej. México UTC-6: el 20 de marzo local empieza a
    las 06:00 UTC del 20 y termina a las 05:59:59.999999 UTC del 21 -- sin
    esto, un pago cobrado por la tarde/noche cae en el corte del día
    siguiente y el reporte no cuadra con el dinero físico en la caja.
    """
    tz = tz or _zona_horaria()
    inicio_local = datetime.combine(fecha_local, time.min, tzinfo=tz)
    fin_local = datetime.combine(fecha_local, time.max, tzinfo=tz)
    return (
        inicio_local.astimezone(timezone.utc).replace(tzinfo=None),
        fin_local.astimezone(timezone.utc).replace(tzinfo=None),
    )


def periodo_escolar_actual() -> str:
    """
    Etiqueta del periodo escolar VIGENTE según la convención de
    cuatrimestres de la institución: A = Ene-Abr, B = May-Ago, C = Sep-Dic.
    Es solo una SUGERENCIA precargada en los formularios de cobros -- el
    campo periodo_escolar sigue siendo texto libre editable a mano, así
    que si algún día cambia la convención no rompe nada, solo deja de
    adivinar bien.
    """
    hoy = hoy_local()
    if hoy.month <= 4:
        letra = 'A'
    elif hoy.month <= 8:
        letra = 'B'
    else:
        letra = 'C'
    return f'{hoy.year}-{letra}'


from decimal import Decimal, InvalidOperation
from functools import wraps

import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from urllib.parse import urlparse

from flask import (
    Flask, render_template, request, flash, redirect, url_for, abort,
    session, send_file, send_from_directory, current_app
)
from flask_login import (
    UserMixin, login_user, logout_user,
    login_required, current_user
)
from flask_mail import Message
from sqlalchemy import UniqueConstraint, CheckConstraint, Index, text, or_, func
from sqlalchemy.exc import IntegrityError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from markupsafe import Markup, escape

from config import config_by_name
from extensiones import db, migrate, login_manager, csrf, limiter, mail


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
    Roles humanos del sistema; los docentes NO tienen rol (regla de
    negocio: cero acceso a maestros).

      - DIRECTIVO: control total. Único que crea/gestiona cuentas y borra
        documentos.
      - ADMINISTRATIVO: acceso a TODO (alumnos + cobros) salvo lo
        exclusivo de Dirección (borrar documentos, usuarios).
      - CONTADOR: acceso COMPLETO al área de Cobros (crear/cancelar
        cargos, catálogo de conceptos, configurar recargos). Sin acceso
        al área de administración de alumnos.
      - CAPTURADOR: acceso a la administración académica del alumno
        (documentos, estatus, boletas, historial). Sin acceso a Cobros.
    """
    DIRECTIVO = 'Dirección / Administrador General'
    ADMINISTRATIVO = 'Administrativo (Alumnos + Cobros)'
    CONTADOR = 'Contador (Área de Cobros)'
    CAPTURADOR = 'Capturador (Administración del Alumno)'


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
    fecha_creacion = db.Column(db.DateTime, default=ahora_utc)
    ultimo_acceso = db.Column(db.DateTime, nullable=True)

    def set_password(self, password_plano: str) -> None:
        # pbkdf2:sha256 (default de Werkzeug): estándar robusto y ampliamente auditado.
        self.password_hash = generate_password_hash(password_plano)

    def check_password(self, password_plano: str) -> bool:
        return check_password_hash(self.password_hash, password_plano)

    def es_directivo(self) -> bool:
        return self.rol == RolUsuario.DIRECTIVO

    def puede_cobros(self) -> bool:
        return self.rol in (RolUsuario.DIRECTIVO, RolUsuario.ADMINISTRATIVO, RolUsuario.CONTADOR)

    def puede_gestionar_cargos(self) -> bool:
        """Crear/cancelar cargos y condonar recargos -- más estrecho que puede_cobros() (Administrativo NO entra aquí)."""
        return self.rol in (RolUsuario.DIRECTIVO, RolUsuario.CONTADOR)

    def puede_alumnos(self) -> bool:
        return self.rol in (RolUsuario.DIRECTIVO, RolUsuario.ADMINISTRATIVO, RolUsuario.CAPTURADOR)

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
    monto_mensualidad = db.Column(db.Numeric(10, 2), nullable=True)  # Precio de mensualidad de esta carrera (solo Directivo lo edita)
    activo = db.Column(db.Boolean, default=True, nullable=False)
    fecha_creacion = db.Column(db.DateTime, default=ahora_utc)

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
        nullable=False,
        index=True  # PERFORMANCE-NOTE (H4): filtrado constantemente vía
                    # Materia.query.filter_by(id_plan_fk=...) -- "Escudo del Plan"
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
        nullable=False,
        index=True  # PERFORMANCE-NOTE (H4): se filtra por carrera/plan en
                    # reportes y listados constantemente
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

    fecha_registro = db.Column(db.DateTime, default=ahora_utc)
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
        hoy = ahora_utc().date()
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

    def saldo_total_adeudado(self):
        """
        Suma el saldo pendiente de TODOS los cargos no cancelados del
        alumno. Se define aquí (no en Cargo) porque necesita recorrer
        todos los cargos de este alumno específico.
        """
        return sum(
            (c.saldo_pendiente() for c in self.cargos if c.estatus != EstatusCargo.CANCELADO),
            Decimal('0.00')
        )

    def tiene_adeudo(self) -> bool:
        return self.saldo_total_adeudado() > 0


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
    fecha_subida = db.Column(db.DateTime, default=ahora_utc)

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
        nullable=False,
        index=True  # PERFORMANCE-NOTE (H4): filtrado constante para boleta/historial del alumno
    )
    id_materia_fk = db.Column(
        db.Integer,
        db.ForeignKey('materias.id'),
        nullable=False,
        index=True  # PERFORMANCE-NOTE (H4): filtrado para reportes por materia
    )

    calificacion_final = db.Column(db.Float, nullable=False)
    periodo_escolar = db.Column(db.String(20), nullable=False)  # Ej: "2025-B", "Ene-Abr 2025"

    fecha_captura = db.Column(db.DateTime, default=ahora_utc)
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
    fecha = db.Column(db.DateTime, default=ahora_utc)

    alumno = db.relationship('Alumno', backref=db.backref('historial_estatus', lazy=True, order_by='HistorialEstatus.fecha.desc()'))
    usuario = db.relationship('Usuario')

    def __repr__(self):
        return f'<HistorialEstatus {self.matricula_fk}: {self.estatus_anterior} -> {self.estatus_nuevo}>'


class HistorialCalificacion(db.Model):
    """
    Auditoría de cada vez que se crea o modifica una calificación --
    quién la capturó/corrigió, cuándo, y cuál era el valor anterior
    (nulo si era una captura nueva, no una corrección). Nunca se
    sobreescribe ni se borra, ni siquiera desde la carga masiva.
    """
    __tablename__ = 'historial_calificaciones'

    id = db.Column(db.Integer, primary_key=True)

    matricula_fk = db.Column(db.String(20), db.ForeignKey('alumnos.matricula_id'), nullable=False)
    id_materia_fk = db.Column(db.Integer, db.ForeignKey('materias.id'), nullable=False)
    periodo_escolar = db.Column(db.String(20), nullable=False)

    usuario_fk = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)

    calificacion_anterior = db.Column(db.Float, nullable=True)
    calificacion_nueva = db.Column(db.Float, nullable=False)
    fecha = db.Column(db.DateTime, default=ahora_utc)

    materia = db.relationship('Materia')
    usuario = db.relationship('Usuario')

    def __repr__(self):
        return f'<HistorialCalificacion {self.matricula_fk}: {self.calificacion_anterior} -> {self.calificacion_nueva}>'


class InscripcionMateria(db.Model):
    """
    "Carga académica" de un alumno: qué materias tiene asignadas en un
    periodo escolar dado. Se genera automáticamente al activarlo por
    primera vez (junto con sus cargos de inscripción), para las materias
    de su plan+cuatrimestre EN ESE MOMENTO -- respeta el Escudo del Plan
    de Estudios igual que Calificacion.

    Es un registro histórico (snapshot): si el plan de estudios cambia
    después, esta tabla sigue reflejando lo que realmente se le asignó
    al alumno en su momento, no lo que el plan diga hoy.

    grupo queda opcional a propósito -- deja la puerta abierta a asignar
    grupo/maestro/horario por materia más adelante sin rediseñar nada.
    """
    __tablename__ = 'inscripciones_materia'

    id = db.Column(db.Integer, primary_key=True)

    matricula_fk = db.Column(
        db.String(20),
        db.ForeignKey('alumnos.matricula_id'),
        nullable=False,
        index=True  # PERFORMANCE-NOTE (H4): carga académica se consulta por alumno constantemente
    )
    id_materia_fk = db.Column(
        db.Integer,
        db.ForeignKey('materias.id'),
        nullable=False,
        index=True
    )
    periodo_escolar = db.Column(db.String(20), nullable=False)

    grupo = db.Column(db.String(20), nullable=True)
    fecha_inscripcion = db.Column(db.DateTime, default=ahora_utc)

    alumno = db.relationship('Alumno', backref=db.backref('inscripciones_materia', lazy=True))
    materia = db.relationship('Materia')

    __table_args__ = (
        UniqueConstraint(
            'matricula_fk', 'id_materia_fk', 'periodo_escolar',
            name='uq_inscripcion_alumno_materia_periodo'
        ),
    )

    def __repr__(self):
        return f'<InscripcionMateria {self.matricula_fk}: materia {self.id_materia_fk} ({self.periodo_escolar})>'


# ---------------------------------------------------------------------------
# SISTEMA DE COBROS (colegiaturas, inscripción, etc.)
# Alcance deliberadamente acotado: cargos y pagos por alumno (NO es un
# sistema contable de activos/pasivos de la universidad — eso, si algún
# día se necesita, sería un módulo aparte y mucho más grande).
# ---------------------------------------------------------------------------

class EstatusCargo(enum.Enum):
    PENDIENTE = 'Pendiente'
    PARCIAL = 'Pago Parcial'
    PAGADO = 'Pagado'
    CANCELADO = 'Cancelado'


class MetodoPago(enum.Enum):
    EFECTIVO = 'Efectivo'
    TRANSFERENCIA = 'Transferencia Bancaria'
    TARJETA = 'Tarjeta'
    DEPOSITO = 'Depósito Bancario'
    OTRO = 'Otro'


class TipoRecargo(enum.Enum):
    """Cada universidad calcula sus recargos distinto — por eso es configurable."""
    MONTO_FIJO = 'Monto fijo (una sola vez)'
    PORCENTAJE = 'Porcentaje del adeudo'
    POR_DIA = 'Monto fijo por cada día de atraso'
    PORCENTAJE_MENSUAL = 'Porcentaje acumulativo por cada mes de atraso'


class TipoDescuentoBeca(enum.Enum):
    """Cada institución maneja sus becas distinto — por eso soporta ambos mecanismos, no uno fijo."""
    PORCENTAJE = 'Porcentaje'
    MONTO_FIJO = 'Monto Fijo'


class ConceptoCobro(db.Model):
    """
    Catálogo de conceptos de cobro (Colegiatura, Inscripción, Servicio
    Social, Uniformes, Recargo, etc.). Se administra desde la interfaz —
    NUNCA se escribe como texto libre al capturar un cargo, para que los
    reportes por concepto sean siempre consistentes.
    """
    __tablename__ = 'conceptos_cobro'
    __table_args__ = (
        db.UniqueConstraint('nombre', name='uq_conceptos_cobro_nombre'),
    )

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    monto_sugerido = db.Column(db.Numeric(10, 2), nullable=True)  # Precio de referencia; se puede ajustar a mano en cada cargo
    es_mensualidad = db.Column(db.Boolean, nullable=False, default=False)  # True = usa el precio por carrera (PlanEstudio.monto_mensualidad) en vez de monto_sugerido
    activo = db.Column(db.Boolean, default=True, nullable=False)
    fecha_creacion = db.Column(db.DateTime, default=ahora_utc)

    def __repr__(self):
        return f'<ConceptoCobro {self.nombre}>'


class Beca(db.Model):
    """
    Beca o descuento otorgado a un alumno, aplicable ÚNICAMENTE a su
    Colegiatura/Mensualidad -- nunca a Inscripción, Uniformes, u otros
    conceptos. Cada institución maneja sus becas distinto (algunas dan
    porcentaje, otras un monto fijo), así que soporta ambos mecanismos
    en vez de forzar uno.

    Tiene vigencia por periodo_escolar (ej. "2026-B") -- NO es indefinida
    por defecto: si sigue aplicando el siguiente periodo, hay que
    otorgarla de nuevo explícitamente. Esto es a propósito, para que
    nadie se quede con un descuento activo por accidente después de que
    debería haber terminado.
    """
    __tablename__ = 'becas'

    id = db.Column(db.Integer, primary_key=True)
    matricula_fk = db.Column(db.String(20), db.ForeignKey('alumnos.matricula_id'), nullable=False)

    nombre = db.Column(db.String(100), nullable=False)  # Ej. "Beca Excelencia Académica", "Descuento Hermanos"
    tipo_descuento = db.Column(db.Enum(TipoDescuentoBeca), nullable=False)
    valor = db.Column(db.Numeric(10, 2), nullable=False)  # 20 (=20%) si es PORCENTAJE, o 500.00 si es MONTO_FIJO

    periodo_escolar = db.Column(db.String(20), nullable=False)  # Vigente para este periodo y los meses que empiecen con él

    activa = db.Column(db.Boolean, nullable=False, default=True)

    otorgada_por_fk = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    fecha_otorgada = db.Column(db.DateTime, default=ahora_utc)
    motivo = db.Column(db.String(255), nullable=True)

    alumno = db.relationship('Alumno', backref=db.backref('becas', lazy=True, order_by='Beca.fecha_otorgada.desc()'))
    otorgada_por = db.relationship('Usuario')

    def calcular_descuento(self, monto_base):
        """Cuánto se descuenta de un monto base -- nunca más de lo que vale el cargo."""
        if self.tipo_descuento == TipoDescuentoBeca.PORCENTAJE:
            descuento = (monto_base * self.valor / Decimal('100')).quantize(Decimal('0.01'))
        else:
            descuento = self.valor
        return min(descuento, monto_base)

    def __repr__(self):
        return f'<Beca {self.nombre} para {self.matricula_fk} ({self.periodo_escolar})>'


class ConfiguracionCobros(db.Model):
    """
    Configuración GLOBAL de recargos por atraso. Es una tabla de una sola
    fila (patrón "singleton"): siempre se usa/edita el único registro que
    exista, vía ConfiguracionCobros.obtener(). Así el recargo es
    "auto-ajustable" por institución sin tocar código — Dirección lo
    cambia desde una pantalla y aplica de inmediato a todos los cargos
    vencidos de ahí en adelante.
    """
    __tablename__ = 'configuracion_cobros'

    id = db.Column(db.Integer, primary_key=True)
    tipo_recargo = db.Column(db.Enum(TipoRecargo), nullable=False, default=TipoRecargo.MONTO_FIJO)
    valor_recargo = db.Column(db.Numeric(10, 2), nullable=False, default=Decimal('0.00'))
    dias_gracia = db.Column(db.Integer, nullable=False, default=0)  # Días después del vencimiento antes de recargar

    @staticmethod
    def obtener():
        """Devuelve la única configuración existente, creándola con valores neutros si no existe."""
        config = ConfiguracionCobros.query.first()
        if not config:
            config = ConfiguracionCobros(
                tipo_recargo=TipoRecargo.MONTO_FIJO,
                valor_recargo=Decimal('0.00'),
                dias_gracia=0,
            )
            db.session.add(config)
            db.session.commit()
        return config


class ConfiguracionInstitucion(db.Model):
    """
    Configuración GLOBAL de la institución (patrón "singleton", igual que
    ConfiguracionCobros). Esto es lo que hace que el sistema sirva para
    CUALQUIER tipo de escuela sin tocar código: la estructura de datos
    (un "programa" con periodos numerados, cada periodo con sus materias)
    ya es genérica -- un "grado" de primaria es estructuralmente igual a
    un "cuatrimestre" de una carrera. Lo único que cambiaba de escuela a
    escuela era CÓMO SE LLAMAN esas cosas, y eso es justo lo que vive aquí.

    Por ejemplo, para una universidad: nombre_periodo="Cuatrimestre",
    nombre_programa="Carrera". Para una primaria: nombre_periodo="Grado",
    nombre_programa="Nivel Educativo". El resto del sistema (boletas,
    cargos automáticos, carga académica, avanzar de periodo) funciona
    exactamente igual sin importar qué tipo de escuela sea.
    """
    __tablename__ = 'configuracion_institucion'

    id = db.Column(db.Integer, primary_key=True)

    nombre_institucion = db.Column(db.String(150), nullable=False, default='Mi Institución Educativa')

    # Cómo se le llama a cada periodo numerado (Cuatrimestre / Semestre / Grado / Año / Trimestre / Nivel...)
    nombre_periodo_singular = db.Column(db.String(40), nullable=False, default='Cuatrimestre')
    nombre_periodo_plural = db.Column(db.String(40), nullable=False, default='Cuatrimestres')

    # Cómo se le llama al "programa" que agrupa varios periodos (Carrera / Grado Escolar / Nivel Educativo...)
    nombre_programa_singular = db.Column(db.String(40), nullable=False, default='Carrera')
    nombre_programa_plural = db.Column(db.String(40), nullable=False, default='Carreras')
    # Género gramatical de nombre_programa, para poder escribir "esa/ese", "la/el" correctamente
    # en vez de adivinarlo por cómo termina la palabra (ej. "programa" termina en "a" pero es
    # MASCULINO en español -- adivinar por terminación produce textos mal escritos).
    programa_es_femenino = db.Column(db.Boolean, nullable=False, default=True)  # "Carrera" -> femenino

    max_periodos = db.Column(db.Integer, nullable=False, default=9)  # Reemplaza a CUATRIMESTRES_MAXIMOS fijo en config.py

    @staticmethod
    def obtener():
        """Devuelve la única configuración existente, creándola con los valores de universidad (los actuales) si no existe."""
        config = ConfiguracionInstitucion.query.first()
        if not config:
            config = ConfiguracionInstitucion()
            db.session.add(config)
            db.session.commit()
        return config

    @property
    def articulo_programa(self):
        """'la' o 'el', según el género configurado -- para que las plantillas nunca tengan que adivinar."""
        return 'la' if self.programa_es_femenino else 'el'

    @property
    def esa_programa(self):
        """'esa' o 'ese'."""
        return 'esa' if self.programa_es_femenino else 'ese'

    @property
    def otra_programa(self):
        """'otra' o 'otro'."""
        return 'otra' if self.programa_es_femenino else 'otro'


class Cargo(db.Model):
    """
    Un cobro pendiente para un alumno (colegiatura de un mes, inscripción,
    reinscripción, materiales, etc.). Puede pagarse en una sola exhibición
    o en varios pagos parciales — el saldo y el estatus se calculan
    siempre a partir de la suma de sus Pago asociados, nunca se guardan
    "a mano", para que nunca queden desincronizados.
    """
    __tablename__ = 'cargos'

    id = db.Column(db.Integer, primary_key=True)
    matricula_fk = db.Column(
        db.String(20),
        db.ForeignKey('alumnos.matricula_id'),
        nullable=False,
        index=True  # PERFORMANCE-NOTE (H4): estado de cuenta / cobros se consultan por matrícula constantemente
    )

    concepto_cobro_fk = db.Column(db.Integer, db.ForeignKey('conceptos_cobro.id'), nullable=True)
    concepto = db.Column(db.String(150), nullable=False)  # Denormalizado: nombre del concepto al momento de crear el cargo
    monto = db.Column(db.Numeric(10, 2), nullable=False)
    recargo_aplicado = db.Column(db.Numeric(10, 2), nullable=False, default=Decimal('0.00'))
    recargo_congelado = db.Column(db.Boolean, nullable=False, default=False)  # True = Dirección lo condonó/ajustó a mano; ya no se recalcula solo
    periodo_escolar = db.Column(db.String(20), nullable=True)
    fecha_vencimiento = db.Column(db.Date, nullable=True, index=True)  # PERFORMANCE-NOTE (H4): filtrado en cartera vencida/dashboard
    fecha_generacion = db.Column(db.DateTime, default=ahora_utc)
    estatus = db.Column(db.Enum(EstatusCargo), default=EstatusCargo.PENDIENTE, nullable=False, index=True)  # PERFORMANCE-NOTE (H4): filtrado en casi todos los reportes de cobros
    comentario = db.Column(db.String(255), nullable=True)  # Ej. motivo de cancelación

    generado_por_fk = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)

    alumno = db.relationship(
        'Alumno',
        backref=db.backref('cargos', lazy=True, order_by='Cargo.fecha_generacion.desc()')
    )
    generado_por = db.relationship('Usuario')
    concepto_cobro = db.relationship('ConceptoCobro')

    __table_args__ = (
        # Respaldo en BD de _cargo_duplicado(): un cargo NO cancelado es
        # único por (alumno, concepto del catálogo, periodo escolar) --
        # ver migración b7e2c9a41f3d para el razonamiento completo
        # (por qué es un índice PARCIAL -- excluye CANCELADO -- y por qué
        # usa COALESCE(periodo_escolar, '') en vez de la columna directa).
        Index(
            'uq_cargos_activo_matricula_concepto_periodo',
            'matricula_fk', 'concepto_cobro_fk', text("COALESCE(periodo_escolar, '')"),
            unique=True,
            sqlite_where=text("estatus != 'CANCELADO'"),
            postgresql_where=text("estatus != 'CANCELADO'"),
        ),
    )

    def total_pagado(self):
        """Suma solo los pagos NO anulados -- un pago anulado ya no cuenta para el saldo."""
        return sum((p.monto_pagado for p in self.pagos if not p.anulado), Decimal('0.00'))

    def saldo_pendiente(self):
        return (self.monto + self.recargo_aplicado) - self.total_pagado()

    def actualizar_estatus(self):
        """Recalcula el estatus a partir de los pagos reales. Nunca se sobreescribe a mano."""
        if self.estatus == EstatusCargo.CANCELADO:
            return
        saldo = self.saldo_pendiente()
        if saldo <= 0:
            self.estatus = EstatusCargo.PAGADO
        elif self.total_pagado() > 0:
            self.estatus = EstatusCargo.PARCIAL
        else:
            self.estatus = EstatusCargo.PENDIENTE

    def esta_vencido(self):
        """Calculado en tiempo real (no se guarda) para no depender de un job en segundo plano."""
        return (
            self.estatus not in (EstatusCargo.PAGADO, EstatusCargo.CANCELADO)
            and self.fecha_vencimiento is not None
            and self.fecha_vencimiento < hoy_local()
        )

    def actualizar_recargo_si_vencido(self, config=None):
        """
        Recalcula el recargo con la configuración VIGENTE (auto-ajustable:
        si Dirección cambia la fórmula, aplica de inmediato a partir de
        aquí). El recargo nunca DISMINUYE aunque la config cambie a la
        baja — solo puede crecer o quedarse igual, para no borrar
        recargos que ya se hicieron oficiales en algún reporte previo.
        Se llama cada vez que se listan los cargos de un alumno (sin
        necesidad de un cron job en segundo plano).

        PERFORMANCE-NOTE: acepta un `config` ya cargado (ConfiguracionCobros)
        para quien esté procesando muchos cargos en un loop (ej. reportes de
        cartera vencida / dashboard) -- así se evita que cada cargo dispare
        su propia llamada a ConfiguracionCobros.obtener(), que es siempre
        la misma fila. Si no se pasa, se comporta igual que antes (una
        sola llamada, para el caso normal de un solo cargo).
        """
        if self.estatus in (EstatusCargo.PAGADO, EstatusCargo.CANCELADO):
            return
        if not self.fecha_vencimiento:
            return
        if self.recargo_congelado:
            return  # Dirección ya ajustó este recargo a mano -- no se recalcula solo

        if config is None:
            config = ConfiguracionCobros.obtener()
        hoy = hoy_local()
        dias_de_atraso = (hoy - self.fecha_vencimiento).days - config.dias_gracia

        if dias_de_atraso <= 0:
            return  # todavía dentro del periodo de gracia: no se aplica ningún recargo

        if config.tipo_recargo == TipoRecargo.MONTO_FIJO:
            recargo_calculado = config.valor_recargo
        elif config.tipo_recargo == TipoRecargo.PORCENTAJE:
            recargo_calculado = (self.monto * config.valor_recargo / Decimal('100')).quantize(Decimal('0.01'))
        elif config.tipo_recargo == TipoRecargo.POR_DIA:
            recargo_calculado = (config.valor_recargo * dias_de_atraso).quantize(Decimal('0.01'))
        elif config.tipo_recargo == TipoRecargo.PORCENTAJE_MENSUAL:
            # Ej.: mensualidad $2,000, valor_recargo=10 -> se suman $200 por
            # cada mes COMPLETO de atraso (mes 1: $200, mes 2: $400, etc.).
            # Aproximamos "mes" como bloques de 30 días de atraso.
            meses_de_atraso = -(-dias_de_atraso // 30)  # redondeo hacia arriba, mínimo 1
            recargo_calculado = (
                self.monto * config.valor_recargo / Decimal('100') * meses_de_atraso
            ).quantize(Decimal('0.01'))
        else:
            recargo_calculado = Decimal('0.00')

        if recargo_calculado > self.recargo_aplicado:
            self.recargo_aplicado = recargo_calculado

    def __repr__(self):
        return f'<Cargo {self.matricula_fk}: {self.concepto} - ${self.monto}>'


class Pago(db.Model):
    """Un pago (total o parcial) aplicado a un Cargo específico."""
    __tablename__ = 'pagos'
    __table_args__ = (
        db.UniqueConstraint('folio', name='uq_pagos_folio'),
    )

    id = db.Column(db.Integer, primary_key=True)
    cargo_fk = db.Column(
        db.Integer,
        db.ForeignKey('cargos.id'),
        nullable=False,
        index=True  # PERFORMANCE-NOTE (H4): base de la agregación de saldo por cargo (ver _matriculas_con_adeudo / _calcular_cartera_vencida)
    )

    folio = db.Column(db.String(30), nullable=True)  # Ej. "PAGO-2026-000042"
    monto_pagado = db.Column(db.Numeric(10, 2), nullable=False)
    fecha_pago = db.Column(db.DateTime, default=ahora_utc, index=True)  # PERFORMANCE-NOTE (H4): reportes diarios y dashboard filtran por rango de fecha
    metodo_pago = db.Column(db.Enum(MetodoPago), nullable=False, default=MetodoPago.EFECTIVO)
    referencia = db.Column(db.String(100), nullable=True)  # Folio/número de referencia bancaria
    comentario = db.Column(db.String(255), nullable=True)

    capturado_por_fk = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)

    # Anulación: un pago NUNCA se borra ni se edita -- si alguien lo capturó
    # mal (monto equivocado, alumno equivocado), se marca como anulado con
    # motivo obligatorio, y deja de contar para el saldo del cargo. Queda
    # visible en el historial para siempre, tachado, con quién y por qué.
    anulado = db.Column(db.Boolean, nullable=False, default=False)
    fecha_anulacion = db.Column(db.DateTime, nullable=True)
    anulado_por_fk = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    motivo_anulacion = db.Column(db.String(255), nullable=True)

    cargo = db.relationship(
        'Cargo',
        backref=db.backref('pagos', lazy=True, order_by='Pago.fecha_pago.desc()')
    )
    capturado_por = db.relationship('Usuario', foreign_keys=[capturado_por_fk])
    anulado_por = db.relationship('Usuario', foreign_keys=[anulado_por_fk])

    def __repr__(self):
        return f'<Pago {self.cargo_fk}: ${self.monto_pagado}>'


class ContadorFolio(db.Model):
    """
    Contador atómico para folios que se comparten entre varias filas de
    otra tabla (ej. numero_acta: UN folio cubre TODAS las materias que
    se guardan en un mismo envío de boleta -- por eso no puede resolverse
    con una UniqueConstraint directa sobre Calificacion.numero_acta, a
    diferencia de Pago.folio que sí es 1:1 con su fila).

    Un renglón por (tipo, año) -- ej. ('ACTA', 2026) -- para que el
    consecutivo reinicie cada año, igual que ya hacía _siguiente_numero_acta()
    antes de este cambio (formato "ACTA-2026-000042").

    CONCURRENCIA: mismo patrón que generar_matricula() (ver esa función
    para el razonamiento completo). with_for_update() bloquea la fila en
    motores que lo soportan (PostgreSQL, producción); en SQLite se ignora
    explícitamente porque no hay bloqueo por fila. La protección real y
    definitiva contra folios duplicados, en cualquier motor, es el
    reintento ante IntegrityError en siguiente_folio() de abajo -- cubre
    incluso el caso límite de que dos transacciones intenten INSERTAR la
    primera fila del contador de un (tipo, año) exactamente al mismo
    tiempo, donde no hay ninguna fila previa que bloquear.
    """
    __tablename__ = 'contadores_folio'

    id = db.Column(db.Integer, primary_key=True)
    tipo = db.Column(db.String(30), nullable=False)  # Ej. 'ACTA'. Futuro: otros folios compartidos.
    anio = db.Column(db.Integer, nullable=False)
    ultimo_valor = db.Column(db.Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint('tipo', 'anio', name='uq_contador_folio_tipo_anio'),
    )

    def __repr__(self):
        return f'<ContadorFolio {self.tipo}-{self.anio}: {self.ultimo_valor}>'


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

MESES_LARGOS_ES = [
    '', 'enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio',
    'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre',
]


def _filtro_fechahora(valor, formato='%d/%m/%Y %H:%M'):
    """|fechahora -- DateTime guardado en UTC, mostrado en hora local."""
    local = a_local(valor)
    return local.strftime(formato) if local else '—'


def _filtro_fecha(valor, formato='%d/%m/%Y'):
    """
    |fecha -- sirve tanto para DateTime (se convierte a local primero)
    como para Date puro (NO se convierte: ya es local por diseño).
    """
    if valor is None:
        return '—'
    if isinstance(valor, datetime):
        return a_local(valor).strftime(formato)
    return valor.strftime(formato)


def _filtro_hora(valor, formato='%H:%M'):
    """|hora -- solo la hora local de un DateTime."""
    local = a_local(valor)
    return local.strftime(formato) if local else '—'


def _filtro_fecha_larga(valor):
    """
    |fecha_larga -- "17 de agosto de 2026", con meses en español.
    strftime('%B') depende del locale del sistema operativo, y el VPS de
    producción (Ubuntu sin locale es_MX instalado) lo devuelve en inglés.
    """
    if valor is None:
        return '—'
    d = a_local(valor).date() if isinstance(valor, datetime) else valor
    return f'{d.day} de {MESES_LARGOS_ES[d.month]} de {d.year}'


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
    login_manager.login_view = 'login'
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

    # Aquí se registrarán los Blueprints en pasos posteriores:
    # from routes.registro import registro_bp
    # from routes.admin import admin_bp
    # app.register_blueprint(registro_bp)
    # app.register_blueprint(admin_bp)

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
    if request.endpoint == 'registro':
        flash(
            'Se enviaron demasiadas solicitudes de registro seguidas desde esta '
            'conexión. Por seguridad, espera unos minutos e inténtalo de nuevo.',
            'danger'
        )
        return redirect(url_for('registro'))

    flash('Demasiados intentos de inicio de sesión. Por seguridad, espera un minuto e inténtalo de nuevo.', 'danger')
    return redirect(url_for('login'))


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


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(Usuario, int(user_id))


def es_url_segura(destino: str) -> bool:
    """
    Evita un 'Open Redirect': sin esta validación, alguien podría mandar un
    link tipo /login?next=https://sitio-falso.com y, tras iniciar sesión
    correctamente en el sitio REAL, el usuario terminaría redirigido a un
    sitio externo (útil para phishing dirigido al personal). Solo se
    permite continuar si 'next' es una ruta relativa de este mismo sitio
    (sin esquema http/https ni host propios).
    """
    if not destino:
        return False
    partes = urlparse(destino)
    return not partes.scheme and not partes.netloc


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
            usuario.ultimo_acceso = ahora_utc()
            db.session.commit()

            flash(f'Bienvenido, {usuario.nombre_completo}.', 'success')
            siguiente = request.args.get('next')
            destino = siguiente if es_url_segura(siguiente) else url_for('index')
            return redirect(destino)

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
    usuario = db.get_or_404(Usuario, user_id)

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

def _matriculas_con_adeudo():
    """
    Devuelve el conjunto de matrículas con saldo pendiente > 0, calculado
    con UNA agregación en SQL (subquery de pagos por cargo) en vez de
    cargar cada Alumno y cada Cargo/Pago en Python.

    PERFORMANCE-NOTE: la versión anterior hacía Alumno.query.join(Cargo)...all()
    y luego, para cada alumno, tiene_adeudo() -> saldo_total_adeudado(),
    que a su vez recorre alumno.cargos y, por cada cargo, cargo.pagos
    (ambos lazy='select'). Con pocos alumnos no se nota, pero es un
    patrón N+1 clásico: con miles de alumnos con adeudo, cada carga del
    dashboard dispara cientos/miles de consultas adicionales. Esta versión
    hace el cálculo en 1 sola consulta agregada, sin importar cuántos
    alumnos existan.
    """
    pagos_por_cargo = (
        db.session.query(
            Pago.cargo_fk,
            func.coalesce(func.sum(Pago.monto_pagado), 0).label('total_pagado')
        )
        .filter(Pago.anulado.is_(False))  # Un pago anulado ya no cuenta para el saldo
        .group_by(Pago.cargo_fk)
        .subquery()
    )

    saldo_expr = (
        Cargo.monto + Cargo.recargo_aplicado
        - func.coalesce(pagos_por_cargo.c.total_pagado, 0)
    )

    filas = (
        db.session.query(Cargo.matricula_fk, func.sum(saldo_expr).label('saldo'))
        .outerjoin(pagos_por_cargo, pagos_por_cargo.c.cargo_fk == Cargo.id)
        .filter(Cargo.estatus != EstatusCargo.CANCELADO)
        .group_by(Cargo.matricula_fk)
        .having(func.sum(saldo_expr) > 0)
        .all()
    )
    return {matricula for matricula, _saldo in filas}


def calcular_estadisticas_alumnos():
    """Números clave para el panel del buscador (pantalla de inicio)."""
    con_adeudo = len(_matriculas_con_adeudo())

    return {
        'total': Alumno.query.count(),
        'pendientes': Alumno.query.filter_by(estatus=EstatusAlumno.PENDIENTE).count(),
        'activos': Alumno.query.filter_by(estatus=EstatusAlumno.ACTIVO).count(),
        'documentacion_incompleta': Alumno.query.filter(Alumno.documentacion_pendiente.isnot(None)).count(),
        'faltas': Alumno.query.filter(Alumno.faltas_administrativas.isnot(None)).count(),
        'con_adeudo': con_adeudo,
    }


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


def _max_periodos() -> int:
    """
    Tope de periodos (cuatrimestres/grados/semestres/lo que sea) según
    la configuración de la institución -- reemplaza al antiguo
    _max_periodos(), que era un número fijo en
    config.py y no se podía ajustar por tipo de escuela sin editar código.
    """
    return ConfiguracionInstitucion.obtener().max_periodos


# --- Paginación -------------------------------------------------------------
# PERFORMANCE-NOTE: con ~800 alumnos, el filtro "Activos" del buscador
# generaba una página de MÁS DE 1 MB de HTML (una tarjeta Bootstrap por
# cada alumno). El servidor la armaba rápido (~50ms gracias a los índices
# de H4), pero el navegador tiene que descargar y pintar ese megabyte:
# en una laptop modesta o por WiFi de la escuela, eso sí se siente.
# El cuello de botella no era SQL, era el tamaño de la respuesta.
ALUMNOS_POR_PAGINA = 24   # 24 = 8 filas de 3 tarjetas en pantalla grande
CARGOS_POR_PAGINA = 50


def _paginar_lista(items, page, per_page):
    """
    Pagina una lista que YA está en memoria.

    Se usa donde el orden final no se puede resolver en SQL. Caso concreto:
    la cartera vencida se ordena por saldo pendiente, que es un valor
    calculado (monto + recargo - pagos), no una columna de la tabla.

    Devuelve (items_de_esta_pagina, info_paginacion). El diccionario de
    info expone las mismas llaves que el objeto Pagination de
    Flask-SQLAlchemy (page, pages, total, has_prev, has_next, prev_num,
    next_num), para que las plantillas usen SIEMPRE la misma sintaxis sin
    importar de cuál de los dos venga.
    """
    total = len(items)
    total_paginas = max(1, (total + per_page - 1) // per_page)
    page = min(max(page, 1), total_paginas)  # una página fuera de rango no truena
    inicio = (page - 1) * per_page

    return items[inicio:inicio + per_page], {
        'page': page,
        'per_page': per_page,
        'total': total,
        'pages': total_paginas,
        'has_prev': page > 1,
        'has_next': page < total_paginas,
        'prev_num': page - 1,
        'next_num': page + 1,
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

    NOTA sobre concurrencia: with_for_update() bloquea la fila leída para
    que otra transacción no pueda leer el mismo "último folio" hasta que
    esta termine — esto reduce la condición de carrera en PostgreSQL
    (producción). En SQLite (desarrollo) no hay bloqueo por fila, así que
    la cláusula se ignora silenciosamente sin causar error; por eso la
    protección real y definitiva contra colisiones es la función
    crear_alumno_generando_matricula() de abajo, que reintenta si de
    todos modos ocurre un choque (ej. dos registros "primeros" del mismo
    prefijo, exactamente al mismo tiempo, donde no hay fila que bloquear).
    """
    anio_actual = ahora_utc().year
    prefijo = f"{plan.clave_carrera}{anio_actual}-"

    consulta = (
        Alumno.query
        .filter(Alumno.matricula_id.like(f"{prefijo}%"))
        .order_by(Alumno.matricula_id.desc())
    )

    # with_for_update() (bloqueo de fila) solo tiene efecto real en motores
    # que lo soportan, como PostgreSQL (producción). SQLite (desarrollo)
    # no tiene bloqueo por fila — en vez de asumir que SQLAlchemy lo ignora
    # solo, lo excluimos explícitamente aquí para no depender de un
    # comportamiento de dialecto sin poder verificarlo en este entorno.
    if db.engine.dialect.name != 'sqlite':
        consulta = consulta.with_for_update()

    ultimo = consulta.first()

    if ultimo:
        ultimo_num = int(ultimo.matricula_id.split('-')[-1])
        siguiente = ultimo_num + 1
    else:
        siguiente = 1

    return f"{prefijo}{siguiente:05d}"


def crear_alumno_generando_matricula(plan: 'PlanEstudio', intentos_maximos: int = 3, **datos_alumno):
    """
    Crea y guarda un Alumno generándole matrícula automáticamente, con
    reintentos ante una colisión de matrícula por condición de carrera
    (dos registros al mismo tiempo). Hace su PROPIO commit (por diseño:
    así, si se usa dentro de un bucle de carga masiva, una fila que falla
    nunca arrastra consigo a las filas anteriores que ya se guardaron bien
    — cada una queda comprometida en la base de datos de forma individual).

    Devuelve (alumno, None) si tuvo éxito, o (None, mensaje_error) si
    fallaron todos los intentos. NO valida CURP duplicada ni nada del
    resto de las reglas de negocio — eso debe hacerse ANTES de llamar
    aquí; esta función solo protege la generación de matrícula.
    """
    for _intento in range(intentos_maximos):
        matricula = generar_matricula(plan)
        nuevo_alumno = Alumno(matricula_id=matricula, id_plan_fk=plan.id, **datos_alumno)

        try:
            db.session.add(nuevo_alumno)
            db.session.commit()
            return nuevo_alumno, None
        except IntegrityError:
            db.session.rollback()
            continue  # Probable colisión de matrícula: se reintenta con el siguiente consecutivo

    return None, 'no se pudo generar una matrícula única tras varios intentos; intenta de nuevo'


@app.route('/registro', methods=['GET', 'POST'])
@limiter.limit('20 per hour;5 per minute', methods=['POST'])
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
            plan = db.session.get(PlanEstudio, int(id_plan_raw))
        except (ValueError, TypeError):
            plan = None
        if not plan or not plan.activo:
            errores.append('El plan de estudios seleccionado no es válido.')

    if errores:
        for error in errores:
            flash(error, 'danger')
        # Reenviamos el formulario con los planes para no perder el <select>
        return render_template('registro.html', planes=planes), 400

    alumno, error_creacion = crear_alumno_generando_matricula(
        plan,
        nombre_completo=nombre_completo,
        curp=curp,
        fecha_nacimiento=fecha_nacimiento,
        fecha_certificado_prepa=fecha_certificado_prepa,
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

    if error_creacion:
        flash('Ocurrió un error al guardar tu registro. Verifica tus datos e intenta de nuevo.', 'danger')
        return render_template('registro.html', planes=planes), 400

    # SECURITY-NOTE: esta es la ÚNICA vista PÚBLICA sin login del sistema, así
    # que es la de mayor exposición. Antes la plantilla usaba {{ message|safe }}
    # para poder mostrar <strong>{matricula}</strong> en negritas -- pero eso
    # dejaba la puerta abierta a que un flash() futuro con datos de usuario sin
    # escapar se convirtiera en XSS reflejado. Ahora se arma explícitamente con
    # Markup() + escape(): el HTML fijo (las etiquetas <strong>) se conserva,
    # pero cualquier dato variable (la matrícula) SIEMPRE pasa por escape().
    flash(
        Markup(
            f'¡Registro exitoso! Tu matrícula es <strong>{escape(alumno.matricula_id)}</strong>. '
            'Tu solicitud quedó en estatus "Pendiente de Validación" y será revisada por Control Escolar.'
        ),
        'success'
    )
    return redirect(url_for('registro'))


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


# Firma real (magic bytes) de cada formato aceptado en el expediente.
#
# SECURITY-NOTE (hallazgo #7): la extensión del nombre la elige quien sube
# el archivo, así que por sí sola no prueba nada -- cualquier contenido
# (un .exe, un HTML con <script>) pasaba con solo llamarlo "documento.pdf".
# Estos son los primeros bytes que TODO archivo de ese formato lleva por
# definición del estándar, y no se pueden falsificar sin dejar de ser un
# archivo válido de ese tipo.
#
# Se comprueban a mano en vez de usar python-magic: esa librería necesita
# libmagic (un paquete del sistema operativo, dependencia nueva en el VPS)
# y aquí solo hay 3 formatos, todos con firma fija al inicio.
FIRMAS_POR_EXTENSION = {
    'pdf': (b'%PDF-',),
    'png': (b'\x89PNG\r\n\x1a\n',),
    'jpg': (b'\xff\xd8\xff',),
    'jpeg': (b'\xff\xd8\xff',),
}

BYTES_DE_FIRMA = 8  # Suficiente para la firma más larga (PNG)


def contenido_coincide_con_extension(archivo) -> bool:
    """
    True solo si los primeros bytes del archivo corresponden de verdad al
    formato que anuncia su extensión.

    Se exige coherencia (no basta con que el contenido sea "alguno de los
    permitidos"): el archivo se sirve después con el Content-Type derivado
    de su extensión, y con `X-Content-Type-Options: nosniff` activo en
    Nginx el navegador NO adivina -- un PNG guardado como .pdf
    simplemente no se vería. Mejor rechazarlo al subirlo, con un mensaje
    claro, que descubrirlo el día que alguien necesite el documento.

    Deja el puntero del archivo al inicio: si no se rebobina, el
    archivo.save() posterior guardaría el contenido truncado.
    """
    extension = archivo.filename.rsplit('.', 1)[1].lower() if '.' in archivo.filename else ''
    firmas = FIRMAS_POR_EXTENSION.get(extension)
    if not firmas:
        return False

    inicio = archivo.read(BYTES_DE_FIRMA)
    archivo.seek(0)

    return any(inicio.startswith(firma) for firma in firmas)


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
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def documentos(matricula):
    alumno = db.get_or_404(Alumno, matricula)

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

            # La extensión la escribe quien sube el archivo; los bytes no
            # mienten. Se valida ANTES de escribir nada en disco.
            if not contenido_coincide_con_extension(archivo):
                flash(
                    f'El archivo de "{tipo_doc.value}" no se guardó: su contenido real '
                    f'no coincide con su extensión (no es un PDF/JPG/PNG válido). '
                    f'Si lo renombraste a mano, vuelve a exportarlo o escanearlo en el '
                    f'formato correcto.',
                    'danger'
                )
                continue

            os.makedirs(carpeta_alumno, exist_ok=True)
            nombre_seguro = secure_filename(archivo.filename)
            nombre_final = f'{tipo_doc.name}_{int(ahora_utc().timestamp())}_{nombre_seguro}'
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
        max_cuatrimestres=_max_periodos()
    )


@app.route('/alumno/<matricula>/documento/<int:doc_id>/ver')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def ver_documento(matricula, doc_id):
    """
    Sirve el archivo físico de un documento del expediente, exigiendo
    sesión activa + rol adecuado (a diferencia de servirlo directamente
    desde /static/, que no requiere ningún login).

    SECURITY-NOTE: verificamos explícitamente que el doc_id pedido
    pertenezca a LA MISMA matrícula que viene en la URL. Sin este chequeo,
    alguien con sesión válida pero de bajo rango podría cambiar el doc_id
    en la URL para intentar ver el documento de OTRO alumno cuya matrícula
    no conoce -- aunque ambos estén detrás del mismo control de rol, cada
    documento debe amarrarse a su propio expediente.
    """
    documento = db.get_or_404(DocumentoAlumno, doc_id)
    if documento.matricula_fk != matricula:
        abort(404)

    carpeta_absoluta = os.path.join(
        app.config['UPLOAD_FOLDER'],
        os.path.dirname(documento.ruta_archivo)
    )
    nombre_archivo = os.path.basename(documento.ruta_archivo)

    # send_from_directory ya protege internamente contra path traversal
    # (rutas tipo "../../etc/passwd"), pero igual construimos la ruta a
    # partir de datos que nosotros mismos generamos al subir el archivo
    # (ver documentos()), nunca a partir de un parámetro de la URL.
    return send_from_directory(carpeta_absoluta, nombre_archivo)


@app.route('/documento/<int:doc_id>/eliminar', methods=['POST'])
@rol_requerido('DIRECTIVO')
def eliminar_documento(doc_id):
    """
    Elimina un documento subido por equivocación: borra el archivo físico
    del disco y su registro en la base de datos, y regresa al expediente
    del mismo alumno.
    """
    documento = db.get_or_404(DocumentoAlumno, doc_id)
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


def _cargo_duplicado(matricula, concepto_cobro_id, periodo_escolar):
    """
    Busca un cargo NO cancelado ya existente para el mismo alumno, mismo
    concepto del catálogo y mismo periodo escolar. Se usa tanto al crear
    un cargo individual como al generar mensualidades en lote, para no
    cobrarle dos veces el mismo concepto a la misma persona por error.
    """
    return Cargo.query.filter(
        Cargo.matricula_fk == matricula,
        Cargo.concepto_cobro_fk == concepto_cobro_id,
        Cargo.periodo_escolar == periodo_escolar,
        Cargo.estatus != EstatusCargo.CANCELADO,
    ).first()


def _meses_del_cuatrimestre_actual():
    """[(año, mes), ...] de los 4 meses del cuatrimestre EN CURSO (A=Ene-Abr, B=May-Ago, C=Sep-Dic)."""
    hoy = hoy_local()
    if hoy.month <= 4:
        meses = [1, 2, 3, 4]
    elif hoy.month <= 8:
        meses = [5, 6, 7, 8]
    else:
        meses = [9, 10, 11, 12]
    return [(hoy.year, m) for m in meses]


def _monto_mensualidad_con_beca(alumno, periodo_cargo):
    """
    Monto de mensualidad a cobrar para un cargo de un periodo específico
    (ej. "2026-B-May"), aplicando cualquier beca ACTIVA cuyo periodo de
    vigencia sea un prefijo de ese periodo (una beca con
    periodo_escolar="2026-B" aplica a todos los cargos mensuales de ese
    cuatrimestre: "2026-B-May", "2026-B-Jun", etc.). Si el alumno tiene
    más de una beca vigente al mismo tiempo, se usa la de MAYOR
    descuento (nunca se suman varias becas entre sí).
    """
    monto_base = alumno.plan.monto_mensualidad
    # `is None` a propósito, NO `not monto_base`: una mensualidad de $0.00
    # es un precio CONFIGURADO (la institución decidió que esa carrera es
    # gratuita), mientras que NULL significa "todavía no se captura". Ver
    # _generar_cargos_de_periodo().
    if monto_base is None:
        return None

    becas_vigentes = [b for b in alumno.becas if b.activa and periodo_cargo.startswith(b.periodo_escolar)]
    if not becas_vigentes:
        return monto_base

    mejor_beca = max(becas_vigentes, key=lambda b: b.calcular_descuento(monto_base))
    monto_final = monto_base - mejor_beca.calcular_descuento(monto_base)
    return max(monto_final, Decimal('0.00'))


def _generar_cargos_de_periodo(alumno, nombre_concepto_unico):
    """
    Genera: 1 cargo del concepto indicado ("Inscripción" al activarse por
    primera vez, o "Reinscripción" al avanzar de cuatrimestre) + un cargo
    de mensualidad por cada mes del cuatrimestre EN CURSO únicamente (no
    de toda la carrera): los cuatrimestres futuros se generan uno a la
    vez, cuando toque avanzarlos -- así, si el precio de la mensualidad
    cambia, no queda arrastrado un cargo viejo con el precio equivocado.
    El resto de conceptos (Uniformes, Servicio Social, etc.) se siguen
    agregando a mano. Nunca duplica: reutiliza _cargo_duplicado() antes
    de crear cada cargo.

    LOS PRECIOS SON CONFIGURACIÓN DE LA INSTITUCIÓN, y esta función NUNCA
    los inventa. `ConceptoCobro.monto_sugerido` y
    `PlanEstudio.monto_mensualidad` son nullable a propósito: NULL
    significa "esta institución todavía no capturó ese precio", que NO es
    lo mismo que $0.00 (eso sería "es gratis", una decisión que solo
    Dirección puede tomar desde su pantalla de configuración). Si falta el
    precio no se genera el cargo: se devuelve el aviso para que la persona
    sepa exactamente qué configuración falta y dónde capturarla.

    Devuelve (cargos_generados, avisos_de_configuracion).
    """
    periodo_actual = periodo_escolar_actual()
    vencimiento_default = datetime.strptime(_vencimiento_dia_10_sugerido(), '%Y-%m-%d').date()
    cargos_generados = []
    avisos_de_configuracion = []

    concepto_unico = ConceptoCobro.query.filter_by(nombre=nombre_concepto_unico, activo=True).first()
    if concepto_unico and not _cargo_duplicado(alumno.matricula_id, concepto_unico.id, periodo_actual):
        if concepto_unico.monto_sugerido is None:
            avisos_de_configuracion.append(
                f'No se generó el cargo de "{concepto_unico.nombre}": ese concepto todavía '
                'no tiene precio configurado en el catálogo de la institución. Captúralo en '
                'Conceptos de Cobro y genera el cargo desde la pantalla de Cobros.'
            )
        else:
            cargo = Cargo(
                matricula_fk=alumno.matricula_id,
                concepto_cobro_fk=concepto_unico.id,
                concepto=concepto_unico.nombre,
                monto=concepto_unico.monto_sugerido,
                periodo_escolar=periodo_actual,
                fecha_vencimiento=vencimiento_default,
                estatus=EstatusCargo.PENDIENTE,
            )
            db.session.add(cargo)
            cargos_generados.append(cargo)

    concepto_mensualidad = ConceptoCobro.query.filter_by(es_mensualidad=True, activo=True).first()
    if concepto_mensualidad and (not alumno.plan or alumno.plan.monto_mensualidad is None):
        avisos_de_configuracion.append(
            f'No se generaron los cargos de mensualidad: la carrera '
            f'"{alumno.plan.nombre if alumno.plan else "(sin plan)"}" todavía no tiene '
            'mensualidad configurada. Captúrala en Mensualidades por Carrera.'
        )
    elif concepto_mensualidad:
        for anio, mes in _meses_del_cuatrimestre_actual():
            etiqueta_mes = f'{periodo_actual}-{MESES_ES[mes]}'  # Ej. "2026-B-May" -- único por mes, cabe en 20 caracteres
            if _cargo_duplicado(alumno.matricula_id, concepto_mensualidad.id, etiqueta_mes):
                continue
            cargo = Cargo(
                matricula_fk=alumno.matricula_id,
                concepto_cobro_fk=concepto_mensualidad.id,
                concepto=concepto_mensualidad.nombre,
                monto=_monto_mensualidad_con_beca(alumno, etiqueta_mes),
                periodo_escolar=etiqueta_mes,
                fecha_vencimiento=date(anio, mes, 10),
                estatus=EstatusCargo.PENDIENTE,
            )
            db.session.add(cargo)
            cargos_generados.append(cargo)

    return cargos_generados, avisos_de_configuracion


def _generar_cargos_de_inscripcion(alumno):
    """
    Al activar a un alumno por PRIMERA vez: cargo de Inscripción +
    mensualidades del cuatrimestre en curso. Devuelve
    (cargos_generados, avisos_de_configuracion).
    """
    return _generar_cargos_de_periodo(alumno, 'Inscripción')


def _generar_cargos_de_reinscripcion(alumno):
    """
    Al avanzarlo de cuatrimestre: cargo de Reinscripción + mensualidades
    del nuevo cuatrimestre en curso. Devuelve
    (cargos_generados, avisos_de_configuracion).
    """
    return _generar_cargos_de_periodo(alumno, 'Reinscripción')


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
        .filter_by(id_plan_fk=alumno.id_plan_fk, cuatrimestre=alumno.cuatrimestre_actual)
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
        return False, f'{alumno.nombre_completo} ya está en el último cuatrimestre configurado ({max_cuatri}°).', [], [], []

    alumno.cuatrimestre_actual += 1

    cargos_generados, avisos_de_configuracion = _generar_cargos_de_reinscripcion(alumno)

    # _generar_carga_academica() lee alumno.cuatrimestre_actual, que ya
    # quedó incrementado arriba -- por eso genera la del cuatrimestre NUEVO.
    materias_generadas = _generar_carga_academica(alumno)

    mensaje = f'{alumno.nombre_completo} avanzó al {alumno.cuatrimestre_actual}° cuatrimestre.'
    return True, mensaje, cargos_generados, materias_generadas, avisos_de_configuracion


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


# SECURITY-NOTE: lo que se le dice al personal cuando falla un envío.
# Antes se devolvía str(error), y ese texto se mostraba tal cual en el
# flash del cobro y en la lista de recordatorios fallidos. Una excepción
# de smtplib puede traer el host del servidor de correo, la cuenta usada
# y códigos internos: información de infraestructura que no le sirve a
# quien está cobrando en ventanilla y que no debe quedar en pantalla.
# El detalle completo NO se pierde -- va al log del servidor con
# app.logger.warning(..., exc_info=True), que es donde lo necesita quien
# administra el VPS (logs/sge.log, ver create_app).
MOTIVO_GENERICO_FALLO_CORREO = (
    'No se pudo conectar con el servidor de correo. Revisa la conexión a internet '
    'o la configuración de correo del sistema; el detalle técnico quedó en el log '
    'del servidor.'
)


def enviar_comprobante_pago(alumno, cargo, pago):
    """
    Envía el comprobante de pago al correo del alumno. Si el alumno no
    tiene correo registrado, o si falla el envío (sin internet, SMTP
    caído, etc.), NO debe romper el registro del pago -- el pago ya
    quedó guardado en la BD; solo se avisa al usuario que el correo no
    se pudo mandar.
    """
    if not alumno.correo:
        return False, 'El alumno no tiene correo registrado.'

    try:
        mensaje = Message(
            subject=f'Comprobante de Pago - Folio {pago.folio or ("#" + str(pago.id))}',
            recipients=[alumno.correo],
            body=(
                f'Hola {alumno.nombre_completo},\n\n'
                f'Se registró tu pago con los siguientes datos:\n\n'
                f'Concepto: {cargo.concepto}\n'
                f'Monto pagado: ${pago.monto_pagado}\n'
                f'Fecha: {pago.fecha_pago.strftime("%d/%m/%Y %H:%M")}\n'
                f'Folio: {pago.folio or ("#" + str(pago.id))}\n'
                f'Saldo pendiente del cargo: ${cargo.saldo_pendiente()}\n\n'
                f'Este es un correo automático del Sistema de Gestión Escolar.'
            ),
        )
        mail.send(mensaje)
        return True, None
    except Exception:
        app.logger.warning(
            'Falló el envío del comprobante del pago %s (cargo %s, alumno %s).',
            pago.id, cargo.id, alumno.matricula_id, exc_info=True
        )
        return False, MOTIVO_GENERICO_FALLO_CORREO


DIAS_AVISO_VENCIMIENTO = 3  # Manda el recordatorio cuando falten esta cantidad de días (o menos) para vencer


def enviar_recordatorio_vencimiento(alumno, cargo):
    """Igual que enviar_comprobante_pago: nunca truena, solo reporta si pudo o no."""
    if not alumno.correo:
        return False, 'El alumno no tiene correo registrado.'

    try:
        mensaje = Message(
            subject=f'Recordatorio: {cargo.concepto} próximo a vencer',
            recipients=[alumno.correo],
            body=(
                f'Hola {alumno.nombre_completo},\n\n'
                f'Te recordamos que tienes un pago próximo a vencer:\n\n'
                f'Concepto: {cargo.concepto}\n'
                f'Periodo: {cargo.periodo_escolar or "N/A"}\n'
                f'Saldo pendiente: ${cargo.saldo_pendiente()}\n'
                f'Fecha límite: {cargo.fecha_vencimiento.strftime("%d/%m/%Y")}\n\n'
                f'Después de esta fecha se aplica un recargo por atraso.\n\n'
                f'Este es un correo automático del Sistema de Gestión Escolar.'
            ),
        )
        mail.send(mensaje)
        return True, None
    except Exception:
        app.logger.warning(
            'Falló el envío del recordatorio de vencimiento del cargo %s (alumno %s).',
            cargo.id, alumno.matricula_id, exc_info=True
        )
        return False, MOTIVO_GENERICO_FALLO_CORREO


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


def _calcular_reporte_cobros_del_dia(fecha_reporte):
    """
    Extraída de reporte_cobros_del_dia() para que la vista en pantalla y
    la exportación a Excel usen SIEMPRE el mismo cálculo -- nunca se
    desincronizan entre sí.

    fecha_reporte es un día CALENDARIO LOCAL (el que vivió la caja); como
    Pago.fecha_pago se guarda en UTC, el rango de búsqueda tiene que
    convertirse a UTC con rango_utc_del_dia() -- comparar contra
    medianoche-a-medianoche naive (como si fecha_reporte ya fuera UTC)
    hacía que un pago cobrado por la noche apareciera en el corte del
    día siguiente.
    """
    inicio_dia, fin_dia = rango_utc_del_dia(fecha_reporte)

    pagos_del_dia = (
        Pago.query
        .filter(Pago.fecha_pago >= inicio_dia, Pago.fecha_pago <= fin_dia)
        .order_by(Pago.fecha_pago.asc())
        .all()
    )

    # Los pagos anulados SÍ se muestran en la lista (transparencia total del
    # día), pero NO cuentan en los totales -- ese dinero no se quedó cobrado.
    pagos_vigentes = [p for p in pagos_del_dia if not p.anulado]
    total_del_dia = sum((p.monto_pagado for p in pagos_vigentes), Decimal('0.00'))

    totales_por_concepto = {}
    totales_por_metodo = {}
    for pago in pagos_vigentes:
        concepto = pago.cargo.concepto if pago.cargo else 'Sin concepto'
        totales_por_concepto[concepto] = totales_por_concepto.get(concepto, Decimal('0.00')) + pago.monto_pagado
        totales_por_metodo[pago.metodo_pago.value] = totales_por_metodo.get(pago.metodo_pago.value, Decimal('0.00')) + pago.monto_pagado

    return pagos_del_dia, total_del_dia, totales_por_concepto, totales_por_metodo


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


def _calcular_cartera_vencida():
    """
    Extraída de cartera_vencida() para reutilizar en la exportación a Excel.

    PERFORMANCE-NOTE: la versión anterior hacía Cargo.query...all() y luego,
    por cada cargo candidato, llamaba esta_vencido() / actualizar_recargo_si_vencido()
    / saldo_pendiente() -- cada una de esas dispara cargo.pagos (lazy='select'
    en la relación Cargo.pagos, ver clase Pago), y actualizar_recargo_si_vencido()
    además llamaba ConfiguracionCobros.obtener() en CADA iteración, repitiendo
    la misma consulta de una tabla de una sola fila. Con miles de cargos
    vencidos (justo el escenario de este reporte: "cartera vencida" tiende a
    crecer con el tiempo), esto son miles de consultas extra en una sola
    carga de página.

    Esta versión:
      1. Trae la config UNA sola vez, antes del loop.
      2. Calcula el total pagado de todos los cargos candidatos con UNA
         agregación SQL (mismo patrón que _matriculas_con_adeudo), en vez
         de que cada cargo dispare su propia consulta a Pago.
    """
    hoy = hoy_local()
    config = ConfiguracionCobros.obtener()  # una sola vez, no una vez por cargo

    candidatos = (
        Cargo.query
        .filter(Cargo.estatus.in_([EstatusCargo.PENDIENTE, EstatusCargo.PARCIAL]))
        .filter(Cargo.fecha_vencimiento.isnot(None))
        .filter(Cargo.fecha_vencimiento < hoy)  # ya filtra "vencido" en SQL, no solo en Python
        .all()
    )

    if not candidatos:
        return [], Decimal('0.00')

    cargo_ids = [c.id for c in candidatos]
    pagos_por_cargo = dict(
        db.session.query(
            Pago.cargo_fk,
            func.coalesce(func.sum(Pago.monto_pagado), 0)
        )
        .filter(Pago.cargo_fk.in_(cargo_ids))
        .filter(Pago.anulado.is_(False))
        .group_by(Pago.cargo_fk)
        .all()
    )

    filas = []
    for cargo in candidatos:
        # esta_vencido() ya no hace falta re-evaluarlo: el filtro SQL de
        # arriba (fecha_vencimiento < hoy) más el filtro de estatus ya
        # garantizan que todo lo que llega aquí está vencido.
        cargo.actualizar_recargo_si_vencido(config=config)
        total_pagado = pagos_por_cargo.get(cargo.id, Decimal('0.00'))
        saldo = (cargo.monto + cargo.recargo_aplicado) - total_pagado
        filas.append({
            'cargo': cargo,
            'dias_atraso': (hoy - cargo.fecha_vencimiento).days,
            'saldo': saldo,
        })
    db.session.commit()  # persiste cualquier recargo que se haya actualizado arriba

    filas.sort(key=lambda f: f['saldo'], reverse=True)
    total_vencido = sum((f['saldo'] for f in filas), Decimal('0.00'))

    return filas, total_vencido


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


MESES_ES = ['', 'Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun', 'Jul', 'Ago', 'Sep', 'Oct', 'Nov', 'Dic']


def _rango_ultimos_n_meses(n=6):
    """Lista de diccionarios describiendo cada uno de los últimos n meses (incluye el actual)."""
    hoy = hoy_local()
    rangos = []
    for i in range(n - 1, -1, -1):
        mes_ref = hoy.month - i
        anio_ref = hoy.year
        while mes_ref <= 0:
            mes_ref += 12
            anio_ref -= 1
        inicio = date(anio_ref, mes_ref, 1)
        fin = (date(anio_ref + 1, 1, 1) if mes_ref == 12 else date(anio_ref, mes_ref + 1, 1)) - timedelta(days=1)
        rangos.append({
            'anio': anio_ref,
            'mes': mes_ref,
            'inicio': datetime.combine(inicio, datetime.min.time()),
            'fin': datetime.combine(fin, datetime.max.time()),
            'etiqueta': f'{MESES_ES[mes_ref]} {anio_ref}',
        })
    return rangos


def _calcular_dashboard_cobros():
    """Extraída para reutilizar entre la vista del dashboard y su exportación a Excel."""
    rangos = _rango_ultimos_n_meses(6)

    ingresos_mensuales = []
    for r in rangos:
        total = db.session.query(func.sum(Pago.monto_pagado)).filter(
            Pago.fecha_pago >= r['inicio'], Pago.fecha_pago <= r['fin'], Pago.anulado.is_(False)
        ).scalar() or Decimal('0.00')
        ingresos_mensuales.append({'etiqueta': r['etiqueta'], 'total': total})

    inicio_periodo = rangos[0]['inicio']
    fin_periodo = rangos[-1]['fin']

    ingresos_por_concepto_raw = (
        db.session.query(Cargo.concepto, func.sum(Pago.monto_pagado))
        .join(Pago, Pago.cargo_fk == Cargo.id)
        .filter(Pago.fecha_pago >= inicio_periodo, Pago.fecha_pago <= fin_periodo, Pago.anulado.is_(False))
        .group_by(Cargo.concepto)
        .order_by(func.sum(Pago.monto_pagado).desc())
        .all()
    )
    ingresos_por_concepto = [{'concepto': c, 'total': t} for c, t in ingresos_por_concepto_raw]

    total_cobrado_6_meses = sum((item['total'] for item in ingresos_mensuales), Decimal('0.00'))
    total_cobrado_mes_actual = ingresos_mensuales[-1]['total'] if ingresos_mensuales else Decimal('0.00')

    # Reutiliza el mismo recálculo que Cobros y Cartera Vencida -- nunca
    # se confía en un recargo_aplicado guardado sin refrescar primero.
    #
    # PERFORMANCE-NOTE: mismo patrón N+1 que tenía _calcular_cartera_vencida
    # (ver esa función) -- este dashboard es la pantalla que más seguido se
    # va a abrir, así que se arregla igual: config cargada una sola vez, y
    # el saldo de todos los cargos abiertos calculado con UNA agregación
    # SQL en vez de que cada cargo dispare su propia consulta a Pago.
    hoy = hoy_local()
    config = ConfiguracionCobros.obtener()
    cargos_abiertos = Cargo.query.filter(Cargo.estatus.in_([EstatusCargo.PENDIENTE, EstatusCargo.PARCIAL])).all()
    hubo_cambios = False
    for cargo in cargos_abiertos:
        antes = cargo.recargo_aplicado
        cargo.actualizar_recargo_si_vencido(config=config)
        if cargo.recargo_aplicado != antes:
            hubo_cambios = True
    if hubo_cambios:
        db.session.commit()

    if cargos_abiertos:
        cargo_ids = [c.id for c in cargos_abiertos]
        pagos_por_cargo = dict(
            db.session.query(
                Pago.cargo_fk,
                func.coalesce(func.sum(Pago.monto_pagado), 0)
            )
            .filter(Pago.cargo_fk.in_(cargo_ids))
            .filter(Pago.anulado.is_(False))
            .group_by(Pago.cargo_fk)
            .all()
        )
    else:
        pagos_por_cargo = {}

    total_por_cobrar = Decimal('0.00')
    total_vencido = Decimal('0.00')
    for cargo in cargos_abiertos:
        saldo = (cargo.monto + cargo.recargo_aplicado) - pagos_por_cargo.get(cargo.id, Decimal('0.00'))
        total_por_cobrar += saldo
        if cargo.fecha_vencimiento is not None and cargo.fecha_vencimiento < hoy:
            total_vencido += saldo

    return {
        'ingresos_mensuales': ingresos_mensuales,
        'ingresos_por_concepto': ingresos_por_concepto,
        'total_cobrado_6_meses': total_cobrado_6_meses,
        'total_cobrado_mes_actual': total_cobrado_mes_actual,
        'total_por_cobrar': total_por_cobrar,
        'total_vencido': total_vencido,
    }


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


def _vencimiento_dia_10_sugerido() -> str:
    """
    Fecha sugerida para el vencimiento de mensualidad/reinscripción: el
    alumno tiene del 1 al 10 del mes para pagar. Si ya pasó el día 10 de
    este mes, se sugiere el día 10 del mes siguiente.
    """
    hoy = hoy_local()
    anio, mes = hoy.year, hoy.month
    if hoy.day > 10:
        mes += 1
        if mes > 12:
            mes = 1
            anio += 1
    return date(anio, mes, 10).isoformat()


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


# ---------------------------------------------------------------------------
# MÓDULO DE CAPTURA DE CALIFICACIONES (BOLETA)
# Solo Control Escolar captura, con base en actas físicas firmadas.
# El "Escudo" se aplica filtrando SIEMPRE por id_plan_fk del alumno.
# ---------------------------------------------------------------------------

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


@app.route('/alumno/<matricula>/boleta', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def boleta(matricula):
    alumno = db.get_or_404(Alumno, matricula)

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
        # SECURITY-NOTE: antes venía de un <input> de texto libre en el
        # formulario, así que cualquiera podía escribir el nombre de otra
        # persona ahí -- el registro de auditoría no era confiable. Ahora
        # se toma directo de la sesión autenticada, igual que ya se hacía
        # en Cargo.generado_por_fk y Pago.capturado_por_fk.
        capturado_por = current_user.nombre_completo

        if not periodo_escolar:
            flash('Indica el periodo escolar (ej. "2026-A") antes de guardar.', 'danger')
            return redirect(url_for('boleta', matricula=matricula, cuatrimestre=cuatrimestre_seleccionado))

        # Número de Acta: ya NO es un campo editable -- se genera un folio
        # nuevo por cada envío del formulario (estilo "ACTA-2026-000042",
        # igual que Pago.folio), y se aplica por igual a todas las
        # materias que sí se guarden en este mismo envío.
        numero_acta = _siguiente_numero_acta()

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
                if existente.calificacion_final != valor:
                    _registrar_historial_calificacion(alumno, materia, periodo_escolar, existente.calificacion_final, valor)
                existente.calificacion_final = valor
                existente.numero_acta = numero_acta
                existente.capturado_por = capturado_por
            else:
                _registrar_historial_calificacion(alumno, materia, periodo_escolar, None, valor)
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
        max_cuatrimestres=_max_periodos(),
        calificaciones_existentes=calificaciones_existentes,
        periodo_escolar_sugerido=periodo_escolar_actual(),
    )


@app.route('/boletas/importar')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def boletas_importar():
    """
    Pantalla de carga masiva de calificaciones -- misma idea que la carga
    masiva de alumnos, pero la plantilla es DINÁMICA: una columna por cada
    materia del plan+cuatrimestre elegido, porque cada carrera tiene
    materias distintas.
    """
    planes = PlanEstudio.query.filter_by(activo=True).order_by(PlanEstudio.nombre.asc()).all()
    return render_template(
        'boletas_importar.html',
        planes=planes,
        max_cuatrimestres=_max_periodos(),
        periodo_escolar_sugerido=periodo_escolar_actual(),
    )


@app.route('/boletas/importar/plantilla')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def plantilla_boletas():
    """
    Genera la plantilla .xlsx para el plan+cuatrimestre pedidos: una
    columna por cada materia de ESE plan y cuatrimestre (Escudo del Plan
    de Estudios aplicado también aquí: jamás se mezclan materias de
    otras carreras), con todos los alumnos ACTIVOS de esa carrera
    precargados por matrícula.
    """
    plan_id = request.args.get('plan_id', type=int)
    cuatrimestre = request.args.get('cuatrimestre', type=int)

    plan = db.get_or_404(PlanEstudio, plan_id) if plan_id else None
    if not plan or not cuatrimestre:
        flash('Selecciona carrera y cuatrimestre antes de descargar la plantilla.', 'danger')
        return redirect(url_for('boletas_importar'))

    materias = (
        Materia.query
        .filter_by(id_plan_fk=plan.id, cuatrimestre=cuatrimestre)
        .order_by(Materia.nombre.asc())
        .all()
    )
    if not materias:
        flash(f'"{plan.nombre}" no tiene materias registradas para el {cuatrimestre}° cuatrimestre.', 'danger')
        return redirect(url_for('boletas_importar'))

    alumnos = (
        Alumno.query
        .filter_by(id_plan_fk=plan.id, estatus=EstatusAlumno.ACTIVO)
        .order_by(Alumno.nombre_completo.asc())
        .all()
    )

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Calificaciones'

    fuente_encabezado = Font(bold=True, color='FFFFFF')
    relleno_fijo = PatternFill(start_color='0D6EFD', end_color='0D6EFD', fill_type='solid')
    relleno_materia = PatternFill(start_color='198754', end_color='198754', fill_type='solid')

    encabezados = ['Matrícula', 'Nombre Completo'] + [m.nombre for m in materias]
    for col_idx, nombre_col in enumerate(encabezados, start=1):
        celda = ws.cell(row=1, column=col_idx, value=nombre_col)
        celda.font = fuente_encabezado
        celda.fill = relleno_fijo if col_idx <= 2 else relleno_materia
        ws.column_dimensions[get_column_letter(col_idx)].width = 24

    for row_idx, alumno in enumerate(alumnos, start=2):
        ws.cell(row=row_idx, column=1, value=alumno.matricula_id)
        ws.cell(row=row_idx, column=2, value=alumno.nombre_completo)

    ws.freeze_panes = 'C2'

    ws_guia = wb.create_sheet('Guía')
    ws_guia.append(['Carrera', plan.nombre])
    ws_guia.append(['Cuatrimestre', cuatrimestre])
    ws_guia.append([])
    ws_guia.append(['Instrucciones'])
    ws_guia.append(['No cambies los encabezados ni la columna Matrícula.'])
    ws_guia.append(['Calificación en escala 0-10. Deja en blanco lo que aún no se captura.'])
    ws_guia.append(['El Periodo Escolar y el Número de Acta se capturan UNA vez al subir el archivo, no aquí.'])
    ws_guia.column_dimensions['A'].width = 60

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    nombre_archivo = f'plantilla_boletas_{plan.clave_carrera}_{cuatrimestre}cuatri.xlsx'
    return send_file(
        buffer,
        as_attachment=True,
        download_name=nombre_archivo,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


@app.route('/boletas/importar', methods=['POST'])
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def importar_boletas():
    plan_id = request.form.get('plan_id', type=int)
    cuatrimestre = request.form.get('cuatrimestre', type=int)
    periodo_escolar = request.form.get('periodo_escolar', '').strip()
    # Ya no es texto libre: un folio nuevo por archivo subido, compartido
    # por todas las filas que se guarden en esta carga (ver boleta() y
    # _siguiente_numero_acta() para el mismo criterio en captura individual).
    numero_acta = _siguiente_numero_acta()
    capturado_por = current_user.nombre_completo

    planes = PlanEstudio.query.filter_by(activo=True).order_by(PlanEstudio.nombre.asc()).all()

    plan = db.session.get(PlanEstudio, plan_id) if plan_id else None
    if not plan or not cuatrimestre:
        flash('Selecciona carrera y cuatrimestre.', 'danger')
        return render_template('boletas_importar.html', planes=planes, max_cuatrimestres=_max_periodos(), periodo_escolar_sugerido=periodo_escolar_actual())

    if not periodo_escolar:
        flash('Indica el periodo escolar (ej. "2026-B") antes de subir el archivo.', 'danger')
        return render_template('boletas_importar.html', planes=planes, max_cuatrimestres=_max_periodos(), periodo_escolar_sugerido=periodo_escolar_actual())

    archivo = request.files.get('archivo_excel')
    if not archivo or archivo.filename == '':
        flash('Selecciona el archivo Excel (.xlsx) lleno.', 'danger')
        return render_template('boletas_importar.html', planes=planes, max_cuatrimestres=_max_periodos(), periodo_escolar_sugerido=periodo_escolar_actual())

    if not archivo.filename.lower().endswith('.xlsx'):
        flash('El archivo debe tener formato .xlsx. Usa la plantilla descargable.', 'danger')
        return render_template('boletas_importar.html', planes=planes, max_cuatrimestres=_max_periodos(), periodo_escolar_sugerido=periodo_escolar_actual())

    # ESCUDO DEL PLAN DE ESTUDIOS: las materias del archivo son SIEMPRE las
    # de este plan+cuatrimestre -- nunca se leen materias del archivo por
    # nombre libre sin cruzarlas contra esta consulta.
    materias = (
        Materia.query
        .filter_by(id_plan_fk=plan.id, cuatrimestre=cuatrimestre)
        .order_by(Materia.nombre.asc())
        .all()
    )

    try:
        wb = openpyxl.load_workbook(archivo, data_only=True)
        ws = wb['Calificaciones'] if 'Calificaciones' in wb.sheetnames else wb.active
    except Exception:
        flash('No se pudo leer el archivo. Verifica que sea un .xlsx válido generado con la plantilla.', 'danger')
        return render_template('boletas_importar.html', planes=planes, max_cuatrimestres=_max_periodos(), periodo_escolar_sugerido=periodo_escolar_actual())

    encabezados = [(c.value.strip() if isinstance(c.value, str) else c.value) for c in ws[1]]
    nombres_materias_esperadas = [m.nombre for m in materias]

    if encabezados[:2] != ['Matrícula', 'Nombre Completo'] or encabezados[2:] != nombres_materias_esperadas:
        flash(
            'Las columnas del archivo no coinciden con las materias de '
            f'"{plan.nombre}" - {cuatrimestre}° cuatrimestre. Descarga la '
            'plantilla de nuevo para esta carrera y cuatrimestre exactos '
            '(puede que la hayas descargado para otra combinación).',
            'danger'
        )
        return render_template('boletas_importar.html', planes=planes, max_cuatrimestres=_max_periodos(), periodo_escolar_sugerido=periodo_escolar_actual())

    exitosos = []
    errores = []

    for num_fila, fila in enumerate(ws.iter_rows(min_row=2), start=2):
        if all(c.value in (None, '') for c in fila):
            continue

        matricula = str(fila[0].value or '').strip()
        nombre_mostrado = str(fila[1].value or '').strip()

        alumno = db.session.get(Alumno, matricula) if matricula else None
        if not alumno:
            errores.append({'fila': num_fila, 'matricula': matricula, 'nombre': nombre_mostrado, 'errores': ['Matrícula no encontrada.']})
            continue
        if alumno.id_plan_fk != plan.id:
            errores.append({'fila': num_fila, 'matricula': matricula, 'nombre': alumno.nombre_completo, 'errores': ['Esta matrícula no pertenece a la carrera seleccionada.']})
            continue

        fila_errores = []
        guardadas_de_esta_fila = 0

        for col_idx, materia in enumerate(materias, start=3):
            celda = fila[col_idx - 1] if col_idx - 1 < len(fila) else None
            valor_raw = celda.value if celda is not None else None
            if valor_raw in (None, ''):
                continue

            try:
                valor = float(valor_raw)
            except (ValueError, TypeError):
                fila_errores.append(f'"{materia.nombre}": no es un número válido.')
                continue

            if valor < 0 or valor > 10:
                fila_errores.append(f'"{materia.nombre}": debe estar entre 0 y 10.')
                continue

            # Doble verificación del Escudo a nivel de objeto, por si acaso.
            if not alumno.materia_pertenece_a_su_plan(materia):
                fila_errores.append(f'"{materia.nombre}": no pertenece al plan del alumno (rechazado).')
                continue

            existente = Calificacion.query.filter_by(
                matricula_fk=alumno.matricula_id,
                id_materia_fk=materia.id,
                periodo_escolar=periodo_escolar
            ).first()

            if existente:
                if existente.calificacion_final != valor:
                    _registrar_historial_calificacion(alumno, materia, periodo_escolar, existente.calificacion_final, valor)
                existente.calificacion_final = valor
                existente.numero_acta = numero_acta
                existente.capturado_por = capturado_por
            else:
                _registrar_historial_calificacion(alumno, materia, periodo_escolar, None, valor)
                db.session.add(Calificacion(
                    matricula_fk=alumno.matricula_id,
                    id_materia_fk=materia.id,
                    calificacion_final=valor,
                    periodo_escolar=periodo_escolar,
                    numero_acta=numero_acta,
                    capturado_por=capturado_por,
                ))
            guardadas_de_esta_fila += 1

        # Un commit por ALUMNO: si una fila tiene un error en una materia,
        # las demás materias válidas de esa misma fila SÍ se guardan.
        db.session.commit()

        if guardadas_de_esta_fila > 0:
            exitosos.append({'fila': num_fila, 'matricula': matricula, 'nombre': alumno.nombre_completo, 'guardadas': guardadas_de_esta_fila})
        if fila_errores:
            errores.append({'fila': num_fila, 'matricula': matricula, 'nombre': alumno.nombre_completo, 'errores': fila_errores})

    flash(
        f'{sum(e["guardadas"] for e in exitosos)} calificación(es) guardada(s) en {len(exitosos)} alumno(s). '
        f'{len(errores)} fila(s) con algún error (ver detalle abajo).',
        'success' if exitosos else 'warning'
    )

    return render_template(
        'boletas_importar.html',
        planes=planes,
        max_cuatrimestres=_max_periodos(),
        exitosos=exitosos,
        errores=errores,
        periodo_escolar_sugerido=periodo_escolar_actual(),
    )


@app.route('/alumno/<matricula>/historial-calificaciones')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def historial_calificaciones(matricula):
    """
    Bitácora de auditoría: cada vez que se capturó o corrigió una
    calificación de este alumno, quién lo hizo y cuándo. Cubre tanto la
    captura individual como la carga masiva -- ambas registran aquí.
    """
    alumno = db.get_or_404(Alumno, matricula)
    historial = (
        HistorialCalificacion.query
        .filter_by(matricula_fk=matricula)
        .order_by(HistorialCalificacion.fecha.desc())
        .all()
    )
    return render_template('historial_calificaciones.html', alumno=alumno, historial=historial)


@app.route('/alumno/<matricula>/carga-academica')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def carga_academica(matricula):
    """
    Documento imprimible de la carga académica del alumno: las materias
    que se le asignaron en un periodo dado (por defecto, el periodo más
    reciente que tenga registrado). Se alimenta de InscripcionMateria,
    NO se recalcula contra el plan actual -- así sigue siendo correcto
    aunque el plan de estudios cambie después de que el alumno se inscribió.
    """
    alumno = db.get_or_404(Alumno, matricula)

    periodos_disponibles = sorted({
        i.periodo_escolar for i in
        InscripcionMateria.query.filter_by(matricula_fk=matricula).all()
    }, reverse=True)

    periodo_seleccionado = request.args.get('periodo', '').strip() or (periodos_disponibles[0] if periodos_disponibles else periodo_escolar_actual())

    inscripciones = (
        InscripcionMateria.query
        .filter_by(matricula_fk=matricula, periodo_escolar=periodo_seleccionado)
        .join(Materia)
        .order_by(Materia.nombre.asc())
        .all()
    )

    return render_template(
        'carga_academica.html',
        alumno=alumno,
        inscripciones=inscripciones,
        periodo_seleccionado=periodo_seleccionado,
        periodos_disponibles=periodos_disponibles,
    )


if __name__ == '__main__':
    # NOTA: ya NO se llama db.create_all() aquí. El esquema de la base de
    # datos lo administra exclusivamente Flask-Migrate (flask db upgrade).
    # Tener ambos mecanismos activos a la vez causaba estados inconsistentes
    # entre lo que create_all() creaba y lo que las migraciones esperaban.
    app.run(debug=True)
