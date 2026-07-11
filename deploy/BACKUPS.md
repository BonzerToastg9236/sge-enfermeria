# Respaldos Automáticos — Sistema de Gestión Escolar (SGE)

## Por qué esto es obligatorio antes de cargar alumnos reales

Ahora mismo, si el disco del VPS falla, se llena, o borras algo por error,
**se pierde todo**: la base de datos completa y cada documento escaneado
que Control Escolar haya subido (actas, INEs, comprobantes). No hay forma
de recuperarlo. Con documentación física que ya es difícil de conseguir de
nuevo (como mencionaste), esto no es un "nice to have" — es la diferencia
entre un inconveniente y una catástrofe real para la escuela.

## Qué se respalda y cómo

`deploy/backup.sh` hace 2 cosas cada vez que corre:

1. **`pg_dump`** de toda la base de datos PostgreSQL, comprimido con gzip.
2. **`tar`** de toda la carpeta `static/uploads/` (los documentos subidos).

Ambos se guardan con fecha y hora en el nombre, así que cada corrida deja
un respaldo nuevo sin borrar los anteriores (hasta que la rotación los
limpia — ver abajo).

## El paso que la mayoría se salta (y no debes saltarte tú): respaldo OFFSITE

Un respaldo guardado en el mismo VPS que respalda **no te protege de que
el VPS entero falle, se hackee, o el proveedor tenga un problema** — el
respaldo se va con él. Por eso el script también intenta copiar cada
respaldo a un almacenamiento **externo** usando `rclone` (una herramienta
gratuita que sincroniza con Google Drive, Backblaze B2, Amazon S3, y
muchos más proveedores, con el mismo comando sin importar cuál elijas).

### Configurar rclone (una sola vez)

```bash
sudo apt install -y rclone
rclone config
```

`rclone config` es interactivo — te va a preguntar qué proveedor quieres
usar. Cualquiera de estos funciona bien para este caso (poco espacio, se
sube una vez al día):

- **Backblaze B2** — pensado justo para respaldos, normalmente el más
  barato para este uso. Revisa su tarifa vigente en su sitio antes de
  decidir, cambia de vez en cuando.
- **Google Drive** — si ya tienen una cuenta de Google Workspace de la
  universidad, puede ser la opción más simple de armar.
- **Amazon S3** — más conocido, un poco más caro para uso tan pequeño.

Sea cual sea el que elijas, **cuando `rclone config` te pida un nombre
para el "remote", ponle exactamente `backup`** — el script `backup.sh` ya
está escrito esperando ese nombre. Al terminar, prueba que funcione:

```bash
rclone lsd backup:
```

Si no da error, ya quedó conectado.

## Instalación en el VPS

```bash
cd ~/sge_enfermeria
chmod +x deploy/backup.sh deploy/restore.sh
```

Corre uno manual para probar que sí funciona antes de automatizarlo:

```bash
./deploy/backup.sh
cat ~/backups/backup.log
```

Si ves líneas de "OK" y no "ERROR", vas bien. Revisa también que se hayan
creado los archivos:

```bash
ls -lh ~/backups/
```

## Automatizarlo con cron (que corra solo, todos los días)

```bash
crontab -e
```

Agrega esta línea al final del archivo (corre todos los días a las 3:00 AM,
hora de menor uso):

```
0 3 * * * /home/sge/sge_enfermeria/deploy/backup.sh
```

Guarda y cierra. Verifica que quedó agendado:

```bash
crontab -l
```

De aquí en adelante, no tienes que hacer nada más — cada mañana vas a
tener un respaldo nuevo, local y (si configuraste rclone) también externo.

## Cómo saber si algo falló

Revisa el log de vez en cuando (una vez a la semana es razonable):

```bash
tail -50 ~/backups/backup.log
```

Si quieres que te avisen automáticamente por correo cuando algo falla, es
un paso extra que podemos agregar después (`msmtp` + que cron mande el
resultado por correo) — dímelo si te interesa.

## Cómo restaurar un respaldo

```bash
cd ~/sge_enfermeria
chmod +x deploy/restore.sh   # si no lo hiciste ya

./deploy/restore.sh ~/backups/db_2026-07-15_03-00-00.sql.gz ~/backups/uploads_2026-07-15_03-00-00.tar.gz
```

Te va a pedir confirmación explícita (escribir "si") antes de sobreescribir
nada, precisamente porque es una operación destructiva sobre lo que esté
en producción en ese momento.

## Prueba tu restauración de vez en cuando — en serio

Un respaldo que nunca has probado a restaurar **no es un respaldo
confiable**, es una suposición. Te recomiendo, cada 2-3 meses, correr
`restore.sh` en un VPS de prueba (no en producción) para confirmar que el
proceso completo de verdad funciona y que los datos quedan íntegros. Es
más común de lo que parece descubrir, ya en una emergencia real, que el
respaldo estaba corrupto o incompleto — mejor descubrirlo con calma antes.
