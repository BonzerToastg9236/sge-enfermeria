# Respaldos — Sistema de Gestión Escolar (SGE)

> Probado el 2026-09-20 con PostgreSQL 16: respaldo → **se destruyeron la base y los documentos** →
> restauración desde la copia externa CIFRADA → 7 huellas idénticas (alumnos, cargos, pagos con sus folios,
> bitácora, clave del administrador, versión del esquema y documentos byte a byte).

## Qué se protege y de qué

| Si pasa esto… | ¿Se pierde algo? |
|---|---|
| Se borra algo por error / un dato se corrompe | Como máximo lo capturado desde el último respaldo de la base (**≤ ~9 h**; ver horarios) |
| Falla el disco del servidor | Igual, **si hay copia externa** (sin ella, se pierde todo) |
| Robo, incendio, inundación de la casa | Igual, **si la copia externa está fuera de casa** |
| Ransomware / borrado malicioso | Las copias externas cifradas fuera del servidor sobreviven |
| Se olvida la frase de cifrado **y** el servidor murió | **Las copias externas no se pueden abrir.** Guarda la frase fuera del servidor (ver abajo) |

Lo que queda **sin** proteger: lo capturado entre dos respaldos de la base y los documentos subidos desde
el último respaldo completo (hasta ~24 h). Reducirlo a segundos exige archivado continuo de PostgreSQL (WAL/PITR),
que es un paso posterior y más complejo.

## Qué hace cada script (`deploy/`)

| Script | Qué hace |
|---|---|
| `backup.sh` | `pg_dump` de la base + `tar` de `instance/documentos_alumnos/`. **Valida** cada archivo (gzip íntegro y dump *completo*, no basta con que exista), permisos `600`, y sube una copia **cifrada** (GPG AES-256) con `rclone`. `--solo-bd` respalda solo la base (rápido). |
| `verificar_respaldo.sh` | **Diario (`--frescura`)**: ¿hay respaldo reciente, íntegro y con copia externa al día? **Semanal**: además **restaura de verdad** el último dump en una base temporal y compara tablas con producción. |
| `restore.sh` | Restaura base + documentos, desde un respaldo local **o desde la copia externa cifrada** (`.gpg`) en un servidor nuevo. Antes de sobrescribir guarda copia de lo actual. |
| `avisar.py` | Manda correo si algo falla (y siempre deja `ALERTA_ULTIMO_FALLO.txt` en la carpeta de respaldos). |

## Instalación (una sola vez, como el usuario `sge`)

1. **Autenticación de PostgreSQL** para cron: `~/.pgpass` con permiso `600` (`DEPLOYMENT.md`, sección 12).
2. **Permiso para la verificación semanal** (crea una base temporal, no toca datos):
   `sudo -u postgres psql -c "ALTER USER sge_user CREATEDB;"`
3. **Frase de cifrado** de la copia externa:
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))" > ~/.sge_backup_passphrase
   chmod 600 ~/.sge_backup_passphrase
   cat ~/.sge_backup_passphrase          # <- cópiala AHORA fuera del servidor
   ```
   **Guarda esa frase en un gestor de contraseñas o impresa en un sobre, FUERA del servidor.** Si el servidor
   se pierde y la frase solo estaba ahí, las copias externas son ilegibles para siempre.
4. **Copia externa** con `rclone` (elige Backblaze B2, Google Drive, etc.):
   ```bash
   sudo apt install -y rclone gnupg
   rclone config        # ponle EXACTAMENTE el nombre "backup" al remoto
   rclone lsd backup:   # sin error = conectado
   ```
   Si hay `rclone` pero falta la frase, **no se sube nada** (nunca se sube sin cifrar) y te llega el aviso.
   Sin `rclone` los respaldos quedan solo en este equipo: el sistema lo advierte cada vez.
5. **Avisos por correo** (opcional pero recomendado): en el `.env` de la app agrega
   `ALERTA_CORREO=tu-correo@ejemplo.com` (usa el mismo `MAIL_USERNAME`/`MAIL_PASSWORD` del sistema).
6. **Prueba manual** antes de automatizar:
   ```bash
   cd ~/sge_enfermeria && chmod +x deploy/*.sh
   ./deploy/backup.sh && ./deploy/verificar_respaldo.sh && tail -20 ~/backups/backup.log
   ```

## Automatización (cron del usuario `sge`: `crontab -e`)

```
# Respaldo completo (base + documentos + copia externa cifrada) cada noche
0 3 * * *    /home/sge/sge_enfermeria/deploy/backup.sh
# Solo la base a mediodía y por la tarde: el máximo que se puede perder queda en ~9 horas
0 12,18 * * * /home/sge/sge_enfermeria/deploy/backup.sh --solo-bd
# Cada mañana: ¿corrió el respaldo? (avisa si no)
0 8 * * *    /home/sge/sge_enfermeria/deploy/verificar_respaldo.sh --frescura
# Cada domingo: restauración de PRUEBA en una base temporal
0 4 * * 0    /home/sge/sge_enfermeria/deploy/verificar_respaldo.sh
```

## Servidor en casa: lo que el software no puede hacer por ti

- **Un solo disco es un solo punto de falla.** Lo ideal es un **segundo disco en espejo (RAID 1)**; como mínimo,
  un **disco USB externo** donde se copie `~/backups` cada noche, además de la copia en la nube.
- **No-break (UPS)** para el servidor y el módem; un apagón en plena escritura puede corromper la base.
- Al instalar PostgreSQL conviene activar las sumas de verificación de datos (`initdb --data-checksums`);
  detectan corrupción silenciosa del disco.
- Revisa `~/backups/ALERTA_ULTIMO_FALLO.txt`: si existe, algo falló.

## Restaurar

**Caso normal** (misma máquina, con respaldos locales):
```bash
cd ~/sge_enfermeria
./deploy/restore.sh ~/backups/db_FECHA.sql.gz ~/backups/documentos_FECHA.tar.gz
```

**Desastre** (servidor nuevo, todo perdido; solo tienes la copia externa y tu frase):
1. Instala el sistema en el servidor nuevo siguiendo `DEPLOYMENT.md` **hasta el paso 5** (PostgreSQL, código, `.env`).
   No hace falta correr `seed.py` ni `crear_admin.py`: todo viene en el respaldo.
2. Baja de la nube el `db_*.sql.gz.gpg` y el `documentos_*.tar.gz.gpg` más recientes (`rclone copy backup:sge-backups/ ~/bajados/`).
3. Escribe tu frase en `~/.sge_backup_passphrase` (`chmod 600`).
4. `./deploy/restore.sh ~/bajados/db_FECHA.sql.gz.gpg ~/bajados/documentos_FECHA.tar.gz.gpg`
5. Entra al sistema y verifica alumnos, cobros y un documento.

`restore.sh` valida todo **antes** de tocar nada, guarda una copia de lo que hubiera (`antes_de_restaurar_*.sql.gz`),
restaura con parada ante el primer error, corre `flask db upgrade` y deja los documentos anteriores renombrados
hasta que confirmes que todo está bien.

## Rutina

- **Cada semana**: mira que exista un correo de "verificación OK" o revisa `tail ~/backups/backup.log`.
- **Cada 3 meses**: haz una restauración de prueba **en otra máquina** siguiendo el caso "Desastre". Es lo único
  que prueba también tu frase y tu copia externa.
