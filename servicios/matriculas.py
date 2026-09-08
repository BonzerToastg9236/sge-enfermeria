"""Generación de matrícula única y alta de un Alumno con reintento ante colisión."""

from sqlalchemy.exc import IntegrityError

from extensiones import db
from modelos import Alumno
from utilidades.fechas import ahora_utc


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
