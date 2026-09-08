"""
Fechas y horas en hora local (México). Los 4 filtros de plantilla que las
exponen (|fecha, |fechahora, |hora, |fecha_larga) se agregan a este mismo
archivo en Task 3 -- las funciones de esta primera parte se crean ahora,
antes que el resto de utilidades/, porque modelos/academico.py necesita
ahora_utc como default de columna (Alumno.fecha_registro) y modelos/ se
crea en esta misma tarea.
"""

from datetime import datetime, timezone, date, time
from zoneinfo import ZoneInfo

from flask import current_app

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
    return ZoneInfo(current_app.config.get('ZONA_HORARIA', ZONA_HORARIA_DEFAULT))


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
