"""
Modelos del sistema de cobros: catálogo de conceptos, becas, cargos,
pagos y folios. Cargo.__table_args__ contiene el índice único parcial del
fix #3 (uq_cargos_activo_matricula_concepto_periodo) -- no tocar su
definición al mover esta clase.
"""

import enum
from decimal import Decimal

from sqlalchemy import UniqueConstraint, Index, text

from extensiones import db
from modelos.academico import Alumno, PlanEstudio
from utilidades.fechas import ahora_utc, hoy_local
from utilidades.dinero import MONTO_MAXIMO


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
        # Índice único parcial: a lo más UN concepto puede estar marcado
        # es_mensualidad=True Y activo=True al mismo tiempo. Sin esto,
        # _monto_mensualidad_con_beca() y nuevo_cargo() usan .first() sobre
        # ese filtro -- con dos activos, cuál "gana" seria arbitrario según
        # el orden físico de la tabla (migración
        # b0e4f9d2a1c7_uniq_concepto_mensualidad_activo).
        Index(
            'uq_conceptos_cobro_una_mensualidad_activa', 'es_mensualidad',
            unique=True,
            postgresql_where=text('es_mensualidad = true AND activo = true'),
            sqlite_where=text('es_mensualidad = 1 AND activo = 1'),
        ),
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
        # `or`: un Cargo recién construido (aún sin flush) tiene recargo_aplicado=None.
        return (self.monto + (self.recargo_aplicado or Decimal('0.00'))) - self.total_pagado()

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

        recargo_calculado = min(recargo_calculado, MONTO_MAXIMO)  # nunca más de lo que cabe en Numeric(10,2)

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
