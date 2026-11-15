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
| `tailscale` | Publica `web` en internet por HTTPS con Tailscale Funnel. |

Ningún servicio publica puertos en el host. `db` y `worker` están en una red interna sin salida a internet; `web` y `tailscale` comparten además la red `edge`.

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

## Despliegue

La guía paso a paso para instalarla en un NAS con **OpenMediaVault + Portainer** (imagen construida desde este repositorio) y publicarla por **HTTPS con Tailscale Funnel** (sin dominio propio ni puertos abiertos), incluida la conexión con Immich, está en [docs/deployment.md](docs/deployment.md).

Resumen: carpetas de datos propiedad de `PUID:PGID` → cuenta de Tailscale con HTTPS y Funnel permitidos y una clave en `TS_AUTHKEY` → stack de Portainer «Repository» con las variables de [.env.example](.env.example) → `tailscale funnel --bg http://web:8000` una sola vez.

## App instalable (PWA)

PhotoTag se puede instalar en el móvil como una app. Al abrirla en un móvil, detecta el navegador y propone instalarla (botón en Android/Chrome; instrucciones de «Compartir → Añadir a pantalla de inicio» en iPhone). Requiere HTTPS, que aporta Tailscale Funnel. El service worker solo guarda en el dispositivo los ficheros estáticos y una página «sin conexión»: las fotos y las páginas con datos nunca se cachean.

Los iconos se generan con `python scripts/make_icons.py`.

## Notificaciones

Cada usuario puede activar avisos push en «Mi perfil»: fotos nuevas para catalogar, propuestas pendientes de su voto y sus propuestas resueltas. Se envía un resumen agrupado (como mucho uno por hora y tipo, nunca de 22:00 a 9:00). En iPhone requiere tener la app instalada. Necesita claves VAPID (`python manage.py generate_vapid_keys`); la guía de despliegue explica cómo configurarlas.

## Uso

- **Escanear**: deja los escaneos (JPG, PNG o TIFF) en `scans/inbox`. El worker los procesa en menos de un minuto y mueve el original a `scans/processed` (nunca se borra).
- **Subir desde la app**: cualquier usuario puede pulsar **Subir** (botón flotante en Inicio y Galería) y elegir «Escaneo con varias fotos» o «Foto suelta». Los ficheros van a `scans/inbox` y el worker los procesa igual que los escaneos de la carpeta.
- **Fotos de lado o boca abajo**: el procesado las endereza solo cuando tienen caras. Si no, pulsa **Girar** en la vista de la foto.
- **Duplicados**: un reescaneo o una copia recomprimida de una foto ya existente aparece en la cola de revisión como «posible duplicado». Para revisar las fotos ya procesadas, ejecuta `python manage.py find_duplicates` en el worker.
- **Compartir**: botón **Compartir** en una foto, o **Seleccionar** en la galería (también con pulsación larga) para enviar varias a la vez. En el ordenador se descargan en un ZIP.
- **Eventos**: si el evento no está en la lista, elige «Otro…» y escríbelo. Queda disponible para todos cuando la propuesta se acepta.
- **Ver una foto en detalle**: toca la imagen para abrirla a pantalla completa y hacer zoom; el resto de la app no se puede ampliar con los dedos.
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

## Traducciones

La interfaz está en español (textos fuente) e inglés. Tras añadir o cambiar textos, regenera el catálogo con gettext dentro de la imagen de test (no hace falta instalarlo en local), traduce las entradas nuevas de `locale/en/LC_MESSAGES/django.po` y recompila:

```bash
docker build --target test -t phototag:test .
docker run --rm -v "$PWD":/app -w /app -e DJANGO_DEBUG=1 phototag:test \
  python manage.py makemessages -l en --no-obsolete --ignore=.venv --ignore=tests
# … traducir las entradas nuevas del .po …
docker run --rm -v "$PWD":/app -w /app/locale/en/LC_MESSAGES phototag:test \
  msgfmt --check -o django.mo django.po
```

Commitea el `.po` y el `.mo`.

## Seguridad

- Todas las páginas e imágenes exigen sesión; las imágenes se sirven por una vista de Django, nunca como estáticos.
- Bloqueo de 30 minutos tras 5 intentos de login fallidos (`django-axes`, por usuario o IP real del cliente, que Tailscale pasa en `X-Forwarded-For`).
- Contraseñas de al menos 10 caracteres; no hay registro abierto.
- Ningún secreto en el repositorio: todo por variables de entorno.
