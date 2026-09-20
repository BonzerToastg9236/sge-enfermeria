"""
Fixtures compartidas para toda la suite de pruebas.

Cada test obtiene una base de datos SQLite EN MEMORIA, completamente
limpia y aislada — nunca toca tu instance/sge_dev.db real, así que puedes
correr pytest en cualquier momento sin arriesgar tus datos de desarrollo.

NOTA IMPORTANTE sobre el fixture `app`: en app.py, todas las rutas
(@app.route) están registradas sobre el ÚNICO objeto Flask que se crea al
importar el módulo (`app = create_app()`). Por eso aquí NO se crea una
instancia nueva con create_app('testing') — esa instancia nunca tendría
ninguna ruta registrada encima (los decoradores @app.route ya se
ejecutaron una sola vez, sobre el primer objeto). En vez de eso,
reutilizamos ESE MISMO objeto y le sobreescribimos la configuración para
que apunte a una base de datos en memoria durante las pruebas.
"""

import os
# CRÍTICO: esto tiene que pasar ANTES de "from app import app as flask_app".
# app.py ejecuta `app = create_app(os.environ.get('FLASK_ENV', 'development'))`
# en el momento en que se importa -- si para entonces ya le dijimos que use
# 'testing', la conexión de SQLAlchemy se crea bien desde el primer
# instante, apuntando a memoria. Antes, cambiábamos app.config DESPUÉS de
# que la conexión ya se había creado con la configuración de desarrollo
# (tu archivo real) -- ese cambio tardío no bastaba para que SQLAlchemy
# olvidara la conexión vieja, y create_all()/drop_all() terminaban
# operando sobre instance/sge_dev.db de verdad, sin que se notara. Esto
# es lo que probablemente causó que las tablas desaparecieran varias
# veces durante el desarrollo.
os.environ['FLASK_ENV'] = 'testing'

import pytest
from datetime import date

from app import (
    app as flask_app,
    db as _db,
    limiter as _limiter,
    PlanEstudio,
    Materia,
    Alumno,
    Usuario,
    RolUsuario,
    EstatusAlumno,
)
from config import config_by_name


@pytest.fixture
def app():
    """
    La app real de la aplicación. Ya quedó configurada para pruebas desde
    el momento del import (ver os.environ['FLASK_ENV'] arriba), así que
    aquí ya no hace falta cambiar la configuración en caliente.
    """
    with flask_app.app_context():
        # SEGURO: si por cualquier motivo esto no fuera una base de datos
        # en memoria, es preferible que truene aquí con un mensaje claro a
        # que create_all()/drop_all() operen en silencio sobre tu base de
        # datos real.
        uri_actual = str(_db.engine.url)
        # Solo una BD en memoria, o una BD cuyo nombre contenga "test" indicada a propósito con
        # TEST_DATABASE_URL: la suite hace drop_all() y jamás debe tocar una base real.
        assert 'memory' in uri_actual or (os.environ.get('TEST_DATABASE_URL') and 'test' in _db.engine.url.database), (
            f'¡ALTO! Las pruebas están a punto de usar "{uri_actual}" en vez '
            'de una base de datos en memoria. Revisa FLASK_ENV en conftest.py.'
        )

        _db.create_all()
        _limiter.reset()  # Limpia el contador de intentos de login entre pruebas
        yield flask_app
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def client(app):
    """Cliente de pruebas HTTP (simula peticiones GET/POST sin levantar un servidor real)."""
    return app.test_client()


# ---------------------------------------------------------------------------
# HELPERS DE DATOS DE PRUEBA
# ---------------------------------------------------------------------------

def crear_plan(nombre='Licenciatura de Prueba', clave='TST', anio=2026):
    plan = PlanEstudio(nombre=nombre, clave_carrera=clave, anio_generacion=anio, activo=True)
    _db.session.add(plan)
    _db.session.commit()
    return plan


def crear_materia(plan, nombre='Materia de Prueba', cuatrimestre=1):
    materia = Materia(nombre=nombre, cuatrimestre=cuatrimestre, id_plan_fk=plan.id)
    _db.session.add(materia)
    _db.session.commit()
    return materia


def crear_alumno(plan, curp='ABCD010101HDFXYZ01', nombre='Alumno de Prueba', estatus=EstatusAlumno.ACTIVO, matricula_id=None):
    """
    matricula_id es opcional: por defecto usa el mismo texto fijo de
    siempre ('{clave}{año}-TEST'), para no romper ninguna prueba
    existente. Pero si una prueba necesita 2+ alumnos DEL MISMO plan al
    mismo tiempo (ej. comparar uno Activo contra uno Pendiente), ese
    texto fijo choca -- para esos casos, pásale un matricula_id distinto
    a cada llamada.
    """
    alumno = Alumno(
        matricula_id=matricula_id or f'{plan.clave_carrera}{plan.anio_generacion}-TEST',
        nombre_completo=nombre,
        curp=curp,
        fecha_nacimiento=date(2005, 1, 1),
        fecha_certificado_prepa=date(2023, 7, 1),
        id_plan_fk=plan.id,
        estatus=estatus,
    )
    _db.session.add(alumno)
    _db.session.commit()
    return alumno


def crear_usuario(username='directivo1', password='clave12345', rol=RolUsuario.DIRECTIVO, nombre='Usuario de Prueba'):
    usuario = Usuario(nombre_completo=nombre, username=username, rol=rol, activo=True)
    usuario.set_password(password)
    _db.session.add(usuario)
    _db.session.commit()
    return usuario


def login(client, username, password):
    return client.post(
        '/login',
        data={'username': username, 'password': password},
        follow_redirects=True
    )
