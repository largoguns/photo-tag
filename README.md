# PhotoTag

Catalogación colaborativa de fotos familiares escaneadas. PhotoTag:

1. Separa cada escaneo A4 en fotos individuales y detecta las caras (OpenCV + YuNet).
   Además sugiere quién puede ser cada cara comparándola con las ya identificadas (OpenCV SFace); las sugerencias siempre se confirman por votación.
2. Ofrece una web multiusuario donde la familia propone y vota **quién sale** y **de qué evento es** cada foto.
3. Exporta las fotos catalogadas con metadatos XMP para que **Immich** las importe con las caras ya etiquetadas.

La especificación completa está en [docs/specification.md](docs/specification.md) y la forma de trabajar en [AGENTS.md](AGENTS.md).

## Arquitectura

| Servicio | Función |
|---|---|
| `web` | Django + Gunicorn. Aplica migraciones y crea el superadmin al arrancar. |
| `worker` | Misma imagen. Vigila `/data/scans/inbox`, ejecuta exportaciones y la copia diaria de la BD (APScheduler). |
| `db` | PostgreSQL 16. |
| `cloudflared` | Túnel de Cloudflare hacia `web`. |

Ningún servicio publica puertos en el host. `db` y `worker` están en una red interna sin salida a internet; `web` y `cloudflared` comparten además la red `edge`.

## Desarrollo local

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                                  # SQLite, sin variables de entorno
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Los tests de exportación que validan el XMP necesitan `exiftool`; si no está instalado se omiten. Para ejecutar la suite completa dentro de la imagen (con `exiftool`):

```bash
docker build --target test -t phototag:test . && docker run --rm phototag:test
```

Para levantar el stack en local, copia `.env.example` a `.env`, rellénalo y ejecuta:

```bash
docker compose up --build
```

## Despliegue en el NAS con Portainer

### 1. Carpetas en el NAS

Crea las carpetas de datos y dáselas al usuario con UID/GID 1000 (el usuario no root de la imagen):

```bash
mkdir -p /volume1/phototag/{scans/inbox,scans/processed,photos,export,backups}
chown -R 1000:1000 /volume1/phototag
```

### 2. Túnel de Cloudflare

1. En Cloudflare Zero Trust → *Networks* → *Tunnels*, crea un túnel de tipo **cloudflared** y copia su **token**.
2. Añade un *Public hostname*: `fotos.midominio.com` → servicio `HTTP` → URL `web:8000`.
3. Recomendado: protege el dominio con **Cloudflare Access** como segunda capa (por ejemplo, con lista de emails de la familia).

### 3. Stack

En Portainer → *Stacks* → *Add stack*, elige **Repository** (este repositorio) o pega `docker-compose.yml` en el editor web. En *Environment variables* define:

| Variable | Ejemplo / notas |
|---|---|
| `DJANGO_SECRET_KEY` | Cadena aleatoria larga (`python -c "import secrets; print(secrets.token_urlsafe(50))"`). |
| `DJANGO_ALLOWED_HOSTS` | `fotos.midominio.com` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://fotos.midominio.com` |
| `POSTGRES_PASSWORD` | Contraseña de la BD. |
| `SUPERADMIN_USERNAME` | `admin` |
| `SUPERADMIN_PASSWORD` | Solo necesaria en el primer arranque; después se ignora. |
| `DEFAULT_USER_PASSWORD` | Contraseña genérica inicial de los usuarios nuevos. |
| `TUNNEL_TOKEN` | Token del túnel. |
| `HOST_SCANS_DIR`, `HOST_PHOTOS_DIR`, `HOST_EXPORT_DIR`, `HOST_BACKUPS_DIR` | Rutas del paso 1. |
| `TZ` | `Europe/Madrid` |
| `POLL_INTERVAL_SECONDS`, `MIN_PHOTO_AREA_RATIO`, `FACE_SCORE_THRESHOLD`, `FACE_MATCH_THRESHOLD`, `EMBEDDING_POLL_SECONDS`, `EXPORT_INTERVAL_HOURS` | Opcionales (ver `.env.example`). |

Despliega el stack. Cuando `web` aparezca como *healthy*, entra en `https://fotos.midominio.com` con el superadmin y crea los usuarios en **Más → Usuarios**. Cada usuario deberá cambiar la contraseña genérica en su primer acceso.

### 4. Immich

Immich vive en otro stack y solo necesita leer la carpeta de exportación:

1. En el `docker-compose` de Immich, monta la carpeta de exportación **en solo lectura** en el contenedor `immich-server`, por ejemplo:
   ```yaml
   volumes:
     - /volume1/phototag/export:/mnt/phototag:ro
   ```
2. En Immich → *Administración* → *Bibliotecas externas*, crea una biblioteca con la ruta de importación `/mnt/phototag`.
3. En Immich → *Administración* → *Ajustes* → *Ajustes de metadatos*, activa la **importación de caras desde los metadatos**.
4. Tras cada exportación desde PhotoTag, **reescanea** la biblioteca externa.

Las fotos con caras en los metadatos no pasan por la detección facial automática de Immich; es el comportamiento buscado.

## Uso

- **Escanear**: deja los escaneos (JPG, PNG o TIFF) en `scans/inbox`. El worker los procesa en menos de un minuto y mueve el original a `scans/processed` (nunca se borra).
- **Catalogar**: en la web, abre «Siguiente foto sin catalogar», pulsa cada recuadro de cara para proponer o votar un nombre y propone el evento y la fecha.
- **Sugerencias**: en cuanto una persona está aceptada en alguna foto, las caras parecidas muestran «¿Es …?» en su panel, y la ficha de la persona lista «Puede que aparezca en». Pulsar una sugerencia crea una propuesta con tu voto. Si salen demasiadas sugerencias erróneas, sube `FACE_MATCH_THRESHOLD`; si salen pocas, bájalo.
- **Votación**: una propuesta se acepta con al menos 2 votos a favor y un 66 % de apoyo (configurable en *Administración → Umbrales*). Una foto queda catalogada cuando todas sus caras tienen persona y tiene evento.
- **Exportar**: *Administración → Exportación a Immich → Exportar ahora*. Se exportan solo las fotos catalogadas que han cambiado desde la última exportación, en `export/<año>/` (o `export/sin-fecha/`) junto a su sidecar `.jpg.xmp`.

### Criterios de exportación de fechas

| Fecha aceptada | Carpeta | `exif:DateTimeOriginal` | Etiquetas |
|---|---|---|---|
| Fecha exacta | año | la fecha | — |
| Año | año | 1 de enero de ese año | `Fecha/aproximada` |
| Década | `sin-fecha` | — | `Fecha/aproximada`, `Fecha/Década de 19X0` |
| Sin fecha | `sin-fecha` | — | — |

## Operación

```bash
# Procesar a mano lo pendiente en inbox
docker compose exec worker python manage.py process_scans

# Reprocesar un escaneo (por nombre de fichero o hash). Conserva las propuestas
# y votos de las fotos que siguen existiendo en el mismo sitio del escaneo.
docker compose exec worker python manage.py process_scans --force scan01.tif

# Calcular las huellas de caras que falten (o todas con --all) para las sugerencias
docker compose exec worker python manage.py compute_embeddings

# Exportar sin pasar por la web
docker compose exec worker python manage.py export_photos

# Copia de seguridad inmediata (la automática es diaria a las 03:00, con 14 días de retención)
docker compose exec worker python manage.py backup_db
```

Restaurar una copia (`pg_dump` en formato custom):

```bash
docker compose exec -T db pg_restore -U phototag -d phototag --clean --if-exists < /ruta/phototag-AAAAMMDD-HHMMSS.dump
```

Los escaneos dudosos (menos de 1 o más de 6 fotos, relación de aspecto mayor de 4:1 o recortes solapados) aparecen en *Administración → Cola de revisión*, donde se pueden marcar como correctos o descartar recortes erróneos.

## Seguridad

- Todas las páginas e imágenes exigen sesión; las imágenes se sirven por una vista de Django, nunca como estáticos.
- Bloqueo de 30 minutos tras 5 intentos de login fallidos (`django-axes`, por usuario o IP real de Cloudflare).
- Contraseñas de al menos 10 caracteres; no hay registro abierto.
- Ningún secreto en el repositorio: todo por variables de entorno.
