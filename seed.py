"""
Script de "seed" (datos de prueba).
Ejecutar UNA vez desde la raíz del proyecto:

    python seed.py

Crea un PlanEstudio de ejemplo (Licenciatura en Enfermería) con algunas
materias, para poder probar el registro público y la captura de
calificaciones sin tener que dar de alta todo a mano.
"""

"""
Script de "seed" (datos de prueba).
Ejecutar UNA vez desde la raíz del proyecto:

    python seed.py

Crea varios PlanEstudio de ejemplo (Enfermería + Ingenierías) con algunas
materias cada uno, para poder probar el registro público, el buscador y,
más adelante, la captura de calificaciones sin tener que dar de alta todo
a mano. Es idempotente: si un plan ya existe (misma clave + año), lo omite.
"""

from app import app, db, PlanEstudio, Materia, ConceptoCobro, ConfiguracionCobros, TipoRecargo

# ---------------------------------------------------------------------------
# Definición de planes de estudio de ejemplo.
# Cada plan trae: nombre, clave_carrera (usada en la matrícula), año de
# generación, y un diccionario de materias por cuatrimestre.
# ---------------------------------------------------------------------------
PLANES_DEMO = [
    {
        'nombre': 'Licenciatura en Enfermería',
        'clave_carrera': 'LEN',
        'anio_generacion': 2026,
        'duracion_anios': 3,
        'materias': {
            1: [
                ('Anatomía y Fisiología I', 8.0),
                ('Bioquímica', 8.0),
                ('Fundamentos de Enfermería', 10.0),
                ('Metodología de la Investigación', 6.0),
            ],
            2: [
                ('Anatomía y Fisiología II', 8.0),
                ('Farmacología I', 8.0),
                ('Enfermería del Adulto I', 10.0),
                ('Ética y Legislación en Enfermería', 6.0),
            ],
        },
    },
    {
        'nombre': 'Ingeniería en Sistemas Computacionales',
        'clave_carrera': 'ISC',
        'anio_generacion': 2026,
        'duracion_anios': 4,
        'materias': {
            1: [
                ('Álgebra Superior', 8.0),
                ('Fundamentos de Programación', 10.0),
                ('Introducción a la Ingeniería', 6.0),
                ('Química Básica', 6.0),
            ],
            2: [
                ('Cálculo Diferencial', 8.0),
                ('Programación Orientada a Objetos', 10.0),
                ('Estructura de Datos', 10.0),
                ('Ética Profesional', 4.0),
            ],
        },
    },
    {
        'nombre': 'Ingeniería Industrial',
        'clave_carrera': 'IIN',
        'anio_generacion': 2026,
        'duracion_anios': 4,
        'materias': {
            1: [
                ('Álgebra Superior', 8.0),
                ('Dibujo Industrial', 6.0),
                ('Introducción a la Ingeniería Industrial', 6.0),
                ('Química Básica', 6.0),
            ],
            2: [
                ('Cálculo Diferencial', 8.0),
                ('Estadística I', 8.0),
                ('Procesos de Manufactura', 8.0),
                ('Ética Profesional', 4.0),
            ],
        },
    },
    {
        'nombre': 'Ingeniería Civil',
        'clave_carrera': 'ICI',
        'anio_generacion': 2026,
        'duracion_anios': 4,
        'materias': {
            1: [
                ('Álgebra Superior', 8.0),
                ('Dibujo para Ingeniería Civil', 6.0),
                ('Introducción a la Ingeniería Civil', 6.0),
                ('Química Básica', 6.0),
            ],
            2: [
                ('Cálculo Diferencial', 8.0),
                ('Estática', 8.0),
                ('Topografía', 8.0),
                ('Ética Profesional', 4.0),
            ],
        },
    },
]


def sembrar():
    with app.app_context():
        db.create_all()

        for datos_plan in PLANES_DEMO:
            existente = PlanEstudio.query.filter_by(
                clave_carrera=datos_plan['clave_carrera'],
                anio_generacion=datos_plan['anio_generacion']
            ).first()

            if existente:
                print(f'Ya existe, se omite: {existente}')
                continue

            plan = PlanEstudio(
                nombre=datos_plan['nombre'],
                clave_carrera=datos_plan['clave_carrera'],
                anio_generacion=datos_plan['anio_generacion'],
                duracion_anios=datos_plan.get('duracion_anios'),
                activo=True
            )
            db.session.add(plan)
            db.session.flush()  # Para obtener plan.id antes del commit

            total_materias = 0
            for cuatrimestre, materias in datos_plan['materias'].items():
                for nombre, creditos in materias:
                    db.session.add(Materia(
                        nombre=nombre,
                        cuatrimestre=cuatrimestre,
                        creditos=creditos,
                        id_plan_fk=plan.id
                    ))
                    total_materias += 1

            db.session.commit()
            print(f'Plan creado: {plan} con {total_materias} materias.')

        # --- Catálogo de conceptos de cobro ---
        CONCEPTOS_DEMO = [
            'Inscripción',
            'Reinscripción',
            'Colegiatura',
            'Recargo por Atraso',
            'Servicio Social',
            'Uniformes',
            'Material Didáctico',
        ]
        for nombre in CONCEPTOS_DEMO:
            if not ConceptoCobro.query.filter_by(nombre=nombre).first():
                db.session.add(ConceptoCobro(nombre=nombre, activo=True))
        db.session.commit()
        print(f'Catálogo de conceptos de cobro listo ({len(CONCEPTOS_DEMO)} conceptos).')

        # --- Configuración de recargos (neutral: $0 hasta que Dirección la ajuste) ---
        config = ConfiguracionCobros.obtener()
        print(
            f'Configuración de recargos: {config.tipo_recargo.value}, '
            f'valor ${config.valor_recargo}, {config.dias_gracia} día(s) de gracia. '
            'Ajústala en /configuracion/cobros.'
        )


if __name__ == '__main__':
    sembrar()

