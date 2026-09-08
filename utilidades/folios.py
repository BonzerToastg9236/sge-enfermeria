"""Generación de folios consecutivos y libres de condición de carrera para pagos/documentos."""

from sqlalchemy.exc import IntegrityError

from extensiones import db
from modelos import ContadorFolio
from utilidades.fechas import hoy_local


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
