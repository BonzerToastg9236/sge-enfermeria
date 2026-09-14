"""
Modelos académicos: planes de estudio, materias, alumnos, documentos del
expediente, calificaciones e inscripciones. "Escudo del Plan de Estudios"
vive aquí (ver docstring de Alumno/Materia en el cuerpo movido).
"""

import enum
from datetime import date
from decimal import Decimal

from sqlalchemy import UniqueConstraint, CheckConstraint

from extensiones import db
from modelos.usuarios import Usuario
from utilidades.fechas import ahora_utc


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
        from modelos.cobros import EstatusCargo  # import local a propósito:
        # evita el ciclo con modelos/cobros.py, que sí importa Alumno a
        # nivel de módulo. Ver la nota de Task 2, Step 2 en el plan.
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
