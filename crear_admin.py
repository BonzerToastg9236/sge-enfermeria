"""
Crea el primer usuario DIRECTIVO del sistema (tu perfil, con control total).

Ejecutar UNA vez, después de haber corrido seed.py:

    python crear_admin.py

Te pedirá usuario y contraseña por consola (la contraseña no se muestra en
pantalla mientras la escribes). Con esta cuenta entras a /login y desde ahí
puedes crear más usuarios (Administrativos) en /usuarios/nuevo.
"""

import getpass

from app import app, db, Usuario, RolUsuario

with app.app_context():
    db.create_all()

    print('=== Crear usuario Directivo (Administrador General) ===')
    nombre_completo = input('Nombre completo: ').strip()
    username = input('Usuario (para iniciar sesión, ej. "direccion"): ').strip().lower()

    if not nombre_completo or not username:
        print('El nombre y el usuario no pueden estar vacíos. Vuelve a intentarlo.')
    elif Usuario.query.filter_by(username=username).first():
        print(f'Ya existe un usuario con el nombre de usuario "{username}".')
    else:
        password = getpass.getpass('Contraseña (mínimo 8 caracteres): ')
        confirmar = getpass.getpass('Confirma la contraseña: ')

        if password != confirmar:
            print('Las contraseñas no coinciden. Vuelve a correr el script.')
        elif len(password) < 8:
            print('La contraseña debe tener al menos 8 caracteres.')
        else:
            usuario = Usuario(
                nombre_completo=nombre_completo,
                username=username,
                rol=RolUsuario.DIRECTIVO,
                activo=True
            )
            usuario.set_password(password)
            db.session.add(usuario)
            db.session.commit()
            print(f'\n✅ Usuario directivo "{username}" creado correctamente.')
            print('   Ya puedes entrar en /login con estas credenciales.')
