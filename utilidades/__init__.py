"""Paquete de utilidades sin estado de negocio: fechas, seguridad, archivos, folios, paginación."""

# NO agregar imports/re-exports aquí. modelos/*.py importa utilidades.fechas,
# y utilidades/folios.py y utilidades/seguridad.py importan de modelos: un
# re-export en este archivo cierra ese ciclo y rompe el arranque de la
# aplicación (ImportError por importación circular). Cada submódulo de
# utilidades/ debe seguir importándose por su ruta completa
# (utilidades.fechas, utilidades.folios, etc.), nunca vía utilidades/__init__.py.
