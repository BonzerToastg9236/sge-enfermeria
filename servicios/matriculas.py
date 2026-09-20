"""
Matrículas: formato configurable por institución, generación con reintento ante colisión y alta de un
Alumno (con matrícula automática o con la propia de la institución).
"""

from types import SimpleNamespace

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from extensiones import db
from modelos import Alumno, PlanEstudio, ConfiguracionInstitucion
from utilidades.fechas import ahora_utc

MATRICULA_MAX_CARACTERES = 20   # Alumno.matricula_id (y todas las llaves foráneas que la citan) es String(20)


def formato_vigente():
    """
    Formato de matrícula configurado. Solo LEE: ConfiguracionInstitucion.obtener() haría un commit al
    crear la fila la primera vez, y este código corre dentro de la transacción de un alta. Si la
    configuración aún no existe se usan los valores por defecto (LEN2026-00001).
    """
    config = ConfiguracionInstitucion.query.first()
    if config is not None:
        return config
    return SimpleNamespace(matricula_prefijo='', matricula_incluye_clave=True, matricula_incluye_anio=True,
                           matricula_separador='-', matricula_digitos=5)


def armar_prefijo(config, clave_carrera, anio):
    """
    Parte fija de la matrícula ANTES del consecutivo:
    <prefijo fijo> + <clave de carrera si aplica> + <año si aplica> + <separador>.
    Con la configuración por defecto: "LEN2026-".
    """
    prefijo = config.matricula_prefijo or ''
    if config.matricula_incluye_clave:
        prefijo += clave_carrera
    if config.matricula_incluye_anio:
        prefijo += str(anio)
    return prefijo + (config.matricula_separador or '')


def ejemplo_matricula(config, clave_carrera='LEN', anio=2026, consecutivo=1):
    return f"{armar_prefijo(config, clave_carrera, anio)}{consecutivo:0{config.matricula_digitos}d}"


def formato_matricula_cabe(config, clave_carrera, **cambios):
    """¿La matrícula más larga posible con este formato y esta clave cabe en 20 caracteres? `cambios` prueba un formato propuesto."""
    class _Prueba:
        pass
    prueba = _Prueba()
    for campo in ('matricula_prefijo', 'matricula_incluye_clave', 'matricula_incluye_anio', 'matricula_separador', 'matricula_digitos'):
        setattr(prueba, campo, cambios.get(campo, getattr(config, campo)))
    return len(ejemplo_matricula(prueba, clave_carrera)) <= MATRICULA_MAX_CARACTERES


def generar_matricula(plan: 'PlanEstudio') -> str:
    """
    Genera la matrícula siguiente para un plan dado, según el formato configurado en
    ConfiguracionInstitucion (por defecto <CLAVE><AÑO>-<CONSECUTIVO 5 dígitos>, ej. LEN2026-00001).
    El consecutivo se calcula buscando el MAYOR ya usado con ese mismo prefijo, NO el total de
    alumnos, para que nunca se reutilice un número aunque se den de baja alumnos. Las matrículas
    que la institución trajo con su propio formato (importación) y no terminan en solo dígitos
    después del prefijo simplemente se ignoran.

    NOTA sobre concurrencia: with_for_update() bloquea las filas leídas para
    que otra transacción no pueda leer el mismo "último folio" hasta que
    esta termine — esto reduce la condición de carrera en PostgreSQL
    (producción). En SQLite (desarrollo) no hay bloqueo por fila, así que
    la cláusula se ignora silenciosamente sin causar error; por eso la
    protección real y definitiva contra colisiones es la función
    crear_alumno_generando_matricula() de abajo, que reintenta si de
    todos modos ocurre un choque (ej. dos registros "primeros" del mismo
    prefijo, exactamente al mismo tiempo, donde no hay fila que bloquear).
    """
    config = formato_vigente()
    prefijo = armar_prefijo(config, plan.clave_carrera, ahora_utc().year)

    consulta = (
        Alumno.query
        .filter(Alumno.matricula_id.startswith(prefijo, autoescape=True))
        .order_by(func.length(Alumno.matricula_id).desc(), Alumno.matricula_id.desc())
        .limit(200)
    )

    # with_for_update() (bloqueo de fila) solo tiene efecto real en motores
    # que lo soportan, como PostgreSQL (producción). SQLite (desarrollo)
    # no tiene bloqueo por fila — en vez de asumir que SQLAlchemy lo ignora
    # solo, lo excluimos explícitamente aquí para no depender de un
    # comportamiento de dialecto sin poder verificarlo en este entorno.
    if db.engine.dialect.name != 'sqlite':
        consulta = consulta.with_for_update()

    mayor = 0
    for existente in consulta:
        resto = existente.matricula_id[len(prefijo):]
        if resto.isascii() and resto.isdigit():
            mayor = max(mayor, int(resto))

    return f"{prefijo}{mayor + 1:0{config.matricula_digitos}d}"


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


def crear_alumno_con_matricula(plan: 'PlanEstudio', matricula: str, **datos_alumno):
    """
    Alta con la matrícula que la institución YA tiene asignada (importación). Devuelve (alumno, None)
    o (None, mensaje). Hace su propio commit, igual que crear_alumno_generando_matricula().
    """
    nuevo_alumno = Alumno(matricula_id=matricula, id_plan_fk=plan.id, **datos_alumno)
    try:
        db.session.add(nuevo_alumno)
        db.session.commit()
        return nuevo_alumno, None
    except IntegrityError:
        db.session.rollback()
        return None, f'la matrícula "{matricula}" o la CURP ya existen'
