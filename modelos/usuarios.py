"""Modelos de autenticación: quién puede entrar y con qué rol."""

import enum
import hashlib
import hmac

from flask import current_app

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from extensiones import db
from utilidades.fechas import ahora_utc


class RolUsuario(enum.Enum):
    """
    Roles humanos del sistema; los docentes NO tienen rol (regla de
    negocio: cero acceso a maestros).

      - DIRECTIVO: control total. Único que crea/gestiona cuentas y borra
        documentos.
      - ADMINISTRATIVO: acceso a TODO (alumnos + cobros) salvo lo
        exclusivo de Dirección (borrar documentos, usuarios).
      - CONTADOR: acceso COMPLETO al área de Cobros (crear/cancelar
        cargos, catálogo de conceptos, configurar recargos). Sin acceso
        al área de administración de alumnos.
      - CAPTURADOR: acceso a la administración académica del alumno
        (documentos, estatus, boletas, historial). Sin acceso a Cobros.
    """
    DIRECTIVO = 'Dirección / Administrador General'
    ADMINISTRATIVO = 'Administrativo (Alumnos + Cobros)'
    CONTADOR = 'Contador (Área de Cobros)'
    CAPTURADOR = 'Capturador (Administración del Alumno)'


class Usuario(UserMixin, db.Model):
    """
    Cuenta de acceso al sistema. UserMixin le da a Flask-Login las
    propiedades que necesita (is_authenticated, is_active, get_id, etc.).
    La contraseña NUNCA se guarda en texto plano: solo su hash.
    """
    __tablename__ = 'usuarios'

    id = db.Column(db.Integer, primary_key=True)
    nombre_completo = db.Column(db.String(150), nullable=False)
    username = db.Column(db.String(50), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    rol = db.Column(db.Enum(RolUsuario), nullable=False, default=RolUsuario.ADMINISTRATIVO)
    activo = db.Column(db.Boolean, default=True, nullable=False)
    fecha_creacion = db.Column(db.DateTime, default=ahora_utc)
    ultimo_acceso = db.Column(db.DateTime, nullable=True)

    def huella_sesion(self) -> str:
        """
        Huella derivada del hash de la contraseña (con la SECRET_KEY como llave).
        Va dentro del id de sesión: al cambiar la contraseña la huella cambia y
        toda cookie de sesión/"recordarme" emitida antes deja de valer. La cookie
        es solo firmada (no cifrada), por eso no lleva el hash sino un HMAC.
        """
        llave = (current_app.config.get('SECRET_KEY') or '').encode()
        return hmac.new(llave, (self.password_hash or '').encode(), hashlib.sha256).hexdigest()[:20]

    def get_id(self) -> str:
        return f'{self.id}:{self.huella_sesion()}'

    def set_password(self, password_plano: str) -> None:
        # pbkdf2:sha256 (default de Werkzeug): estándar robusto y ampliamente auditado.
        self.password_hash = generate_password_hash(password_plano)

    def check_password(self, password_plano: str) -> bool:
        return check_password_hash(self.password_hash, password_plano)

    def es_directivo(self) -> bool:
        return self.rol == RolUsuario.DIRECTIVO

    def puede_cobros(self) -> bool:
        return self.rol in (RolUsuario.DIRECTIVO, RolUsuario.ADMINISTRATIVO, RolUsuario.CONTADOR)

    def puede_gestionar_cargos(self) -> bool:
        """Crear/cancelar cargos y condonar recargos -- más estrecho que puede_cobros() (Administrativo NO entra aquí)."""
        return self.rol in (RolUsuario.DIRECTIVO, RolUsuario.CONTADOR)

    def puede_alumnos(self) -> bool:
        return self.rol in (RolUsuario.DIRECTIVO, RolUsuario.ADMINISTRATIVO, RolUsuario.CAPTURADOR)

    @property
    def is_active(self):
        # Sobreescribe el default de UserMixin (que siempre es True):
        # una cuenta desactivada por Dirección no puede iniciar sesión.
        return self.activo

    def __repr__(self):
        return f'<Usuario {self.username} ({self.rol.name})>'
