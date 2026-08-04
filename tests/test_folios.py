"""
Pruebas para siguiente_folio() / _siguiente_numero_acta() -- el fix de la
condición de carrera que antes existía en la generación de folios de acta
("máximo existente + 1", sin protección real).

NOTA SOBRE EL LÍMITE DE ESTAS PRUEBAS: corren contra SQLite en memoria
(igual que el resto de la suite, ver conftest.py), donde with_for_update()
se ignora a propósito (no hay bloqueo por fila en SQLite). Lo que SÍ
prueban aquí es: (1) que la numeración es correcta y consecutiva en uso
normal, (2) que un folio se comparte correctamente entre varias filas del
mismo envío, (3) que tipos distintos llevan contadores independientes.
Lo que NO pueden probar de forma confiable es la protección real ante dos
transacciones concurrentes -- eso depende de with_for_update() funcionando
de verdad, y solo PostgreSQL lo soporta. Antes de confiar en el fix en
producción, correr una prueba de concurrencia equivalente contra un
PostgreSQL real (con threading real y un connection pool que sí permita
conexiones simultáneas), no solo contra SQLite.
"""
from app import ContadorFolio, siguiente_folio, Calificacion, db as _db

from conftest import crear_plan, crear_materia, crear_alumno, crear_usuario


def test_siguiente_folio_es_consecutivo(app):
    """Llamadas sucesivas del mismo tipo deben devolver consecutivos sin huecos."""
    folio_1 = siguiente_folio(tipo='ACTA', prefijo='ACTA')
    _db.session.commit()
    folio_2 = siguiente_folio(tipo='ACTA', prefijo='ACTA')
    _db.session.commit()
    folio_3 = siguiente_folio(tipo='ACTA', prefijo='ACTA')
    _db.session.commit()

    anio = folio_1.split('-')[1]
    assert folio_1 == f'ACTA-{anio}-000001'
    assert folio_2 == f'ACTA-{anio}-000002'
    assert folio_3 == f'ACTA-{anio}-000003'


def test_siguiente_folio_no_duplica_en_llamadas_repetidas(app):
    """Ninguna de N llamadas sucesivas debe repetir folio (caso base, sin concurrencia real)."""
    folios = []
    for _ in range(15):
        folios.append(siguiente_folio(tipo='ACTA', prefijo='ACTA'))
        _db.session.commit()

    assert len(folios) == len(set(folios)), 'Se generaron folios duplicados'


def test_tipos_distintos_llevan_contadores_independientes(app):
    """Un folio de tipo 'ACTA' y uno de tipo hipotético 'BOLETA' no deben interferir entre sí."""
    acta_1 = siguiente_folio(tipo='ACTA', prefijo='ACTA')
    _db.session.commit()
    boleta_1 = siguiente_folio(tipo='BOLETA', prefijo='BOL')
    _db.session.commit()
    acta_2 = siguiente_folio(tipo='ACTA', prefijo='ACTA')
    _db.session.commit()

    assert acta_1.endswith('000001')
    assert boleta_1.endswith('000001')  # empieza su propio conteo en 1, no en 2
    assert acta_2.endswith('000002')


def test_contador_persiste_como_una_sola_fila_por_tipo_y_anio(app):
    """No debe crear una fila nueva de ContadorFolio en cada llamada -- solo la primera vez."""
    for _ in range(5):
        siguiente_folio(tipo='ACTA', prefijo='ACTA')
        _db.session.commit()

    filas = ContadorFolio.query.filter_by(tipo='ACTA').all()
    assert len(filas) == 1
    assert filas[0].ultimo_valor == 5


def test_numero_acta_se_comparte_entre_materias_del_mismo_envio_de_boleta(client):
    """
    Regresión funcional: el folio sigue compartiéndose entre todas las
    materias guardadas en un mismo envío del formulario de boleta -- el
    fix de concurrencia no debe romper esta regla de negocio existente.
    """
    plan = crear_plan()
    materia_1 = crear_materia(plan, nombre='Materia Uno', cuatrimestre=1)
    materia_2 = crear_materia(plan, nombre='Materia Dos', cuatrimestre=1)
    alumno = crear_alumno(plan)
    crear_usuario(username='captura1', password='clave12345')
    client.post('/login', data={'username': 'captura1', 'password': 'clave12345'})

    client.post(f'/alumno/{alumno.matricula_id}/boleta', data={
        'periodo_escolar': '2026-A',
        'cuatrimestre': '1',
        f'calificacion_{materia_1.id}': '8.5',
        f'calificacion_{materia_2.id}': '9.0',
    })

    calificaciones = Calificacion.query.filter_by(matricula_fk=alumno.matricula_id).all()
    assert len(calificaciones) == 2
    numeros_acta = {c.numero_acta for c in calificaciones}
    assert len(numeros_acta) == 1, 'Ambas materias del mismo envío deben compartir el mismo numero_acta'


def test_dos_envios_de_boleta_reciben_numero_acta_distinto(client):
    """Dos envíos separados (aunque sea del mismo alumno) deben recibir folios distintos."""
    plan = crear_plan()
    materia = crear_materia(plan, nombre='Materia Uno', cuatrimestre=1)
    alumno = crear_alumno(plan)
    crear_usuario(username='captura1', password='clave12345')
    client.post('/login', data={'username': 'captura1', 'password': 'clave12345'})

    client.post(f'/alumno/{alumno.matricula_id}/boleta', data={
        'periodo_escolar': '2026-A',
        'cuatrimestre': '1',
        f'calificacion_{materia.id}': '8.0',
    })
    primer_numero_acta = Calificacion.query.filter_by(matricula_fk=alumno.matricula_id).first().numero_acta

    # Corregir la calificación en un segundo envío -- debe quedar con un
    # numero_acta nuevo, no reutilizar el primero.
    client.post(f'/alumno/{alumno.matricula_id}/boleta', data={
        'periodo_escolar': '2026-A',
        'cuatrimestre': '1',
        f'calificacion_{materia.id}': '9.0',
    })
    segundo_numero_acta = Calificacion.query.filter_by(matricula_fk=alumno.matricula_id).first().numero_acta

    assert primer_numero_acta != segundo_numero_acta
