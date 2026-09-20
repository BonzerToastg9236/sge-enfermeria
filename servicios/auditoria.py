"""Registro en la bitácora de auditoría (ver modelos/auditoria.py)."""

from flask_login import current_user

from extensiones import db
from modelos import BitacoraAuditoria


def registrar(accion, entidad=None, entidad_id=None, detalle='', matricula=None):
    """
    Agrega un renglón a la bitácora dentro de la transacción en curso: se
    guarda (o se descarta) junto con la acción que lo origina, así que nunca
    queda una acción sin rastro ni un rastro de una acción que no ocurrió.
    Quien llama sigue siendo responsable del commit.
    """
    usuario = current_user._get_current_object() if current_user and current_user.is_authenticated else None
    db.session.add(BitacoraAuditoria(
        usuario_fk=usuario.id if usuario else None,
        usuario_nombre=usuario.nombre_completo if usuario else None,
        accion=accion,
        entidad=entidad,
        entidad_id=None if entidad_id is None else str(entidad_id),
        matricula_fk=matricula,
        detalle=(detalle or '')[:600],
    ))
