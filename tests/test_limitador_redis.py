"""
Prueba de humo del despliegue (2026-09-20, Gunicorn + PostgreSQL + Redis reales):
con Redis caído, POST /login daba 500 (redis.exceptions.ConnectionError) y
NADIE podía entrar hasta que Redis volviera. En producción el límite de
intentos debe degradar a memoria por proceso (más débil, pero el sistema
sigue en pie) y avisar en el log, no tumbar el login.
"""

from config import ProductionConfig


def test_produccion_no_tumba_el_login_si_redis_falla():
    assert ProductionConfig.RATELIMIT_SWALLOW_ERRORS is True
    assert ProductionConfig.RATELIMIT_IN_MEMORY_FALLBACK_ENABLED is True


def test_produccion_sigue_usando_redis_como_almacen_principal():
    assert ProductionConfig.RATELIMIT_STORAGE_URI.startswith('redis://')
