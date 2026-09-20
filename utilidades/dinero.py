"""
Validación única de importes capturados a mano (cargos, pagos, becas,
precios, recargos, condonaciones).

Antes cada ruta hacía `Decimal(texto)` y `> 0`. Eso aceptaba `Infinity`,
notación científica (`1e30`), dígitos de ancho completo, montos por encima
de lo que cabe en Numeric(10,2) (en PostgreSQL desbordan y dan 500) y más de
dos decimales (0.001 se guardaba como $0.00; 99.995 dejaba un cargo
"Parcial" con saldo 0). Auditoría 2026-09-19.
"""

import re
from decimal import Decimal

# Lo máximo que cabe en Numeric(10, 2).
MONTO_MAXIMO = Decimal('99999999.99')

# Topes de política para la configuración de recargos (no de la columna).
RECARGO_MAXIMO_POR_DIA = Decimal('10000.00')
PORCENTAJE_MAXIMO = Decimal('100')
DIAS_GRACIA_MAXIMOS = 365

# Solo dígitos ASCII con punto decimal opcional: sin signo, sin notación
# científica, sin separadores de miles, sin dígitos de otros alfabetos.
_FORMATO = re.compile(r'[0-9]+(\.[0-9]+)?', re.ASCII)


class MontoInvalido(ValueError):
    """El texto no es un importe utilizable; el mensaje se puede mostrar tal cual."""


def parsear_monto(texto, *, permitir_cero=False, maximo=MONTO_MAXIMO):
    """
    Devuelve el importe como Decimal con exactamente 2 decimales, o lanza
    MontoInvalido. No redondea: un tercer decimal significativo es un error
    de captura, no algo que haya que "arreglar" en silencio.
    """
    crudo = (texto or '').strip()
    if not _FORMATO.fullmatch(crudo):
        raise MontoInvalido('El importe debe ser un número con punto decimal (ej. 1250.50), sin signos ni notación científica.')

    valor = Decimal(crudo)
    if valor != valor.quantize(Decimal('0.01')):
        raise MontoInvalido('El importe admite como máximo 2 decimales.')
    valor = valor.quantize(Decimal('0.01'))

    if valor > maximo:
        raise MontoInvalido(f'El importe no puede ser mayor a ${maximo:,.2f}.')
    if valor == 0 and not permitir_cero:
        raise MontoInvalido('El importe debe ser mayor a 0.')
    return valor


def parsear_entero(texto, *, minimo=0, maximo=1_000_000):
    """Entero ASCII sin signo dentro de [minimo, maximo]; lanza MontoInvalido si no."""
    crudo = (texto or '').strip()
    if not re.fullmatch(r'[0-9]{1,9}', crudo):
        raise MontoInvalido('Debe ser un número entero.')
    valor = int(crudo)
    if not (minimo <= valor <= maximo):
        raise MontoInvalido(f'Debe estar entre {minimo} y {maximo}.')
    return valor


def parsear_calificacion(valor):
    """
    Calificación 0-10 como float. `float()` a secas aceptaba "nan" (que no es
    <0 ni >10 y luego reventaba con 500 al guardar) y "1e1". Acepta el número
    tal como lo entrega Excel (int/float) o texto con punto decimal.
    """
    crudo = str(valor).strip() if valor is not None else ''
    if not _FORMATO.fullmatch(crudo):
        raise MontoInvalido('La calificación debe ser un número con punto decimal (ej. 8.5).')
    numero = float(crudo)
    if numero > 10:
        raise MontoInvalido('La calificación debe estar entre 0 y 10.')
    return numero
