"""
Bitácora de auditoría: quién hizo qué acción sensible y cuándo. Solo se
agrega (no hay ruta para editar ni borrar). usuario_nombre es una copia del
nombre al momento de la acción, para que el registro siga siendo legible aunque
la cuenta se renombre o se desactive. matricula_fk NO es llave foránea a
propósito: el rastro de un alumno debe sobrevivir a cualquier cambio en su
expediente.
"""

from extensiones import db
from utilidades.fechas import ahora_utc


class BitacoraAuditoria(db.Model):
    __tablename__ = 'bitacora_auditoria'

    id = db.Column(db.Integer, primary_key=True)
    fecha = db.Column(db.DateTime, default=ahora_utc, nullable=False, index=True)
    usuario_fk = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    usuario_nombre = db.Column(db.String(150), nullable=True)
    accion = db.Column(db.String(40), nullable=False, index=True)   # Ej. CARGO_CANCELADO
    entidad = db.Column(db.String(40), nullable=True)               # Ej. Cargo
    entidad_id = db.Column(db.String(40), nullable=True)
    matricula_fk = db.Column(db.String(20), nullable=True, index=True)
    detalle = db.Column(db.String(600), nullable=True)

    def __repr__(self):
        return f'<Bitacora {self.accion} {self.entidad}#{self.entidad_id} por {self.usuario_nombre}>'
