"""
Re-exporta todos los modelos y enums para que el resto del proyecto
(servicios/, rutas/, tests/, seed.py, crear_admin.py, y app.py mismo)
pueda seguir escribiendo `from modelos import Alumno, RolUsuario, Cargo`
sin que le importe en qué submódulo vive cada uno.
"""

from modelos.usuarios import RolUsuario, Usuario
from modelos.academico import (
    EstatusAlumno, TurnoAlumno, ModalidadEstudio,
    PlanEstudio, Materia, Alumno,
    TipoDocumento, DocumentoAlumno,
    Calificacion, HistorialEstatus, HistorialCalificacion, InscripcionMateria,
)
from modelos.auditoria import BitacoraAuditoria
from modelos.cobros import (
    EstatusCargo, MetodoPago, TipoRecargo, TipoDescuentoBeca,
    ConceptoCobro, Beca, ConfiguracionCobros, ConfiguracionInstitucion,
    Cargo, Pago, ContadorFolio,
)

__all__ = [
    'RolUsuario', 'Usuario',
    'EstatusAlumno', 'TurnoAlumno', 'ModalidadEstudio',
    'PlanEstudio', 'Materia', 'Alumno',
    'TipoDocumento', 'DocumentoAlumno',
    'Calificacion', 'HistorialEstatus', 'HistorialCalificacion', 'InscripcionMateria',
    'EstatusCargo', 'MetodoPago', 'TipoRecargo', 'TipoDescuentoBeca',
    'ConceptoCobro', 'Beca', 'ConfiguracionCobros', 'ConfiguracionInstitucion',
    'Cargo', 'Pago', 'ContadorFolio',
    'BitacoraAuditoria',
]
