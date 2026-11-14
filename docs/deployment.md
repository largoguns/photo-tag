# Guía de despliegue: OpenMediaVault + Portainer + Cloudflare

Esta guía instala PhotoTag en un NAS con **OpenMediaVault (OMV) sobre un PC x86_64**, con **Portainer** construyendo la imagen directamente desde el repositorio de GitHub, y la publica en internet por **HTTPS (puerto 443)** con un **túnel de Cloudflare**. No hace falta abrir ningún puerto en el router: el túnel sale desde el NAS hacia Cloudflare.

```
Móvil / navegador ──HTTPS 443──► Cloudflare ──túnel cifrado──► cloudflared ──► web:8000
                                                               (en tu NAS, red Docker interna)
```

Tiempo estimado: 45–60 minutos la primera vez.

---

## 0. Requisitos

- OMV 7 con **omv-extras** y **Docker** (plugin *compose* de omv-extras) funcionando.
- **Portainer CE** instalado y accesible en tu red local.
- Un **dominio gestionado por Cloudflare** (los *nameservers* del dominio apuntan a Cloudflare). El plan gratuito es suficiente.
- El repositorio `phototag` en **GitHub** (privado).
- Acceso SSH al NAS (para crear carpetas y comprobar permisos).

Elige ya el subdominio que usará la familia; en esta guía, `fotos.midominio.com`.

---

## 1. Preparar las carpetas en OMV

PhotoTag necesita cuatro carpetas en el NAS:

| Carpeta | Contenido |
|---|---|
| `scans` | Escaneos (`inbox` donde los dejas, `processed` donde quedan los originales) |
| `photos` | Fotos recortadas y miniaturas |
| `export` | Fotos catalogadas con su XMP, para Immich |
| `backups` | Copias diarias de la base de datos |

1. En OMV: **Almacenamiento → Carpetas compartidas → Crear**. Nombre `phototag`, en el disco de datos que prefieras. Guarda y aplica los cambios.
2. En la lista de carpetas compartidas, copia su **ruta absoluta**, del estilo `/srv/dev-disk-by-uuid-1234abcd-…/phototag/`.
3. Por SSH, crea las subcarpetas (sustituye la ruta por la tuya):

   ```bash
   cd /srv/dev-disk-by-uuid-1234abcd-…/phototag
   mkdir -p scans/inbox scans/processed photos export backups
   ```

4. Averigua el UID y GID de tu usuario de OMV (el que usarás para copiar escaneos por SMB):

   ```bash
   id tu_usuario
   # uid=1000(tu_usuario) gid=100(users) …
   ```

   Anota `uid` → será `PUID` y `gid` → será `PGID` (en OMV suele ser `100`, el grupo `users`).

5. Asigna las carpetas a ese usuario:

   ```bash
   chown -R 1000:100 /srv/dev-disk-by-uuid-1234abcd-…/phototag
   chmod -R u+rwX,g+rwX /srv/dev-disk-by-uuid-1234abcd-…/phototag
   ```

6. Opcional: comparte `phototag/scans/inbox` por SMB (**Servicios → SMB/CIFS → Comparticiones**) para dejar los escaneos desde el PC o el propio escáner.

---

## 2. Token de GitHub para que Portainer lea el repositorio

Como el repositorio es privado, Portainer necesita un token de solo lectura.

1. GitHub → tu avatar → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
2. Nombre: `portainer-phototag`. Caducidad: la que prefieras (apúntate renovarlo).
3. **Repository access → Only select repositories →** `phototag`.
4. **Permissions → Repository permissions → Contents: Read-only** (Metadata: Read-only se añade solo).
5. Genera el token y cópialo; no se vuelve a mostrar.

---

## 3. Cloudflare

### 3.1 Crear el túnel

1. Entra en el panel de Cloudflare → **Zero Trust** (la primera vez te pedirá un nombre de equipo y el plan *Free*).
2. **Networks → Tunnels → Create a tunnel**.
3. Tipo **Cloudflared**. Nombre: `phototag-nas`. Guardar.
4. En «Install and run a connector» elige **Docker**. Verás un comando `docker run cloudflare/cloudflared … --token eyJh…`. **No lo ejecutes**: copia solo el valor del token (la cadena larga después de `--token`). Será `TUNNEL_TOKEN`.
5. **Next**. En la pestaña de rutas públicas (*Public hostname*; en versiones recientes del panel aparece como *Published application routes*), añade:

   | Campo | Valor |
   |---|---|
   | Subdomain | `fotos` |
   | Domain | `midominio.com` |
   | Path | *(vacío)* |
   | Service → Type | `HTTP` |
   | Service → URL | `web:8000` |

   `web` es el nombre del servicio en `docker-compose.yml`: cloudflared lo resuelve dentro de la red Docker del stack. No uses la IP del NAS ni `localhost`.

6. Guarda. Cloudflare crea automáticamente el registro DNS `fotos.midominio.com` (CNAME al túnel). El certificado HTTPS y el puerto 443 los pone Cloudflare; el NAS no expone nada.

El túnel aparecerá como *Inactive* hasta que despliegues el stack (paso 4).

### 3.2 Ajustes del dominio (panel normal de Cloudflare, no Zero Trust)

En **tu dominio**:

- **SSL/TLS → Edge Certificates**:
  - **Always Use HTTPS**: activado (redirige http → https; la PWA y el service worker lo necesitan).
  - **Minimum TLS Version**: 1.2.
  - **HSTS**: opcional. Actívalo solo cuando todo funcione y si todos tus subdominios van por HTTPS.
- **SSL/TLS → Overview**: el modo de cifrado (*Full*, *Flexible*…) no afecta al túnel; déjalo en **Full**.
- **Speed → Optimization → Content Optimization → Rocket Loader**: **desactivado**. Reescribe los `<script>` y rompe HTMX y la instalación de la app.
- **Caching**: no hace falta ninguna regla. Las fotos se sirven con `Cache-Control: private` (Cloudflare no las guarda), los estáticos llevan un hash en el nombre y `/sw.js` se sirve con `no-cache`.

### 3.3 Opcional: Cloudflare Access (segunda capa de seguridad)

Pone una pantalla de Cloudflare antes del login de PhotoTag: solo pasan los emails que autorices.

1. Zero Trust → **Access → Applications → Add an application → Self-hosted**.
2. Nombre `PhotoTag`, dominio `fotos.midominio.com`, **Session Duration**: `1 month` (con menos, la app instalada pedirá el código a menudo).
3. Política: **Allow** → *Include* → **Emails** → los emails de la familia.
4. Método de acceso: **One-time PIN** (código por email; no requiere cuentas).

PhotoTag ya está preparada para esto: el manifest de la app se pide con credenciales (`crossorigin="use-credentials"`), así que la instalación funciona con Access activo. Tras activarlo, comprueba la instalación en el móvil (paso 6.4); en iPhone, la app instalada mostrará la pantalla de código de Access la primera vez.

---

## 4. Crear el stack en Portainer

1. Portainer → tu entorno local → **Stacks → Add stack**.
2. **Name**: `phototag` (en minúsculas).
3. **Build method**: **Repository**.
4. Rellena:

   | Campo | Valor |
   |---|---|
   | Repository URL | `https://github.com/<tu-usuario>/phototag` |
   | Repository reference | `refs/heads/main` |
   | Compose path | `docker-compose.yml` |
   | Authentication | activado → *Username*: tu usuario de GitHub, *Personal Access Token*: el del paso 2 |
   | Automatic updates | desactivado (actualizarás a mano, ver paso 8) |

5. **Environment variables → Advanced mode** y pega este bloque con tus valores:

   ```dotenv
   DJANGO_SECRET_KEY=
   DJANGO_ALLOWED_HOSTS=fotos.midominio.com
   DJANGO_CSRF_TRUSTED_ORIGINS=https://fotos.midominio.com
   POSTGRES_PASSWORD=
   SUPERADMIN_USERNAME=admin
   SUPERADMIN_PASSWORD=
   DEFAULT_USER_PASSWORD=
   TUNNEL_TOKEN=
   TZ=Europe/Madrid
   PUID=1000
   PGID=100
   HOST_SCANS_DIR=/srv/dev-disk-by-uuid-1234abcd-…/phototag/scans
   HOST_PHOTOS_DIR=/srv/dev-disk-by-uuid-1234abcd-…/phototag/photos
   HOST_EXPORT_DIR=/srv/dev-disk-by-uuid-1234abcd-…/phototag/export
   HOST_BACKUPS_DIR=/srv/dev-disk-by-uuid-1234abcd-…/phototag/backups
   ```

   Para los secretos, genera valores aleatorios en el NAS por SSH:

   ```bash
   openssl rand -base64 48 | tr -d '\n=/+'; echo   # DJANGO_SECRET_KEY
   openssl rand -base64 24 | tr -d '\n=/+'; echo   # POSTGRES_PASSWORD
   ```

   | Variable | Qué poner |
   |---|---|
   | `DJANGO_SECRET_KEY` | Cadena aleatoria larga (comando de arriba). No la cambies después o se cerrarán todas las sesiones. |
   | `DJANGO_ALLOWED_HOSTS` | El subdominio, **sin** `https://`. |
   | `DJANGO_CSRF_TRUSTED_ORIGINS` | El subdominio **con** `https://`. |
   | `POSTGRES_PASSWORD` | Aleatoria. Solo se usa la primera vez que se crea la base de datos. |
   | `SUPERADMIN_PASSWORD` | Tu contraseña de administrador inicial (mín. 10 caracteres). Solo se usa en el primer arranque. |
   | `DEFAULT_USER_PASSWORD` | Contraseña genérica para los usuarios nuevos; tendrán que cambiarla al entrar. |
   | `TUNNEL_TOKEN` | El token del paso 3.1. |
   | `PUID` / `PGID` | Los del paso 1.4. |
   | `HOST_*_DIR` | Las rutas del paso 1. |

   Opcionales (tienen valor por defecto): `POLL_INTERVAL_SECONDS` (60), `MIN_PHOTO_AREA_RATIO` (0.03), `FACE_SCORE_THRESHOLD` (0.7), `FACE_MATCH_THRESHOLD` (0.363), `EMBEDDING_POLL_SECONDS` (60), `EXPORT_INTERVAL_HOURS` (0 = solo exportación manual).

6. **Deploy the stack**. La primera vez tarda varios minutos: Portainer clona el repositorio y construye la imagen (OpenCV y dependencias).

---

## 5. Comprobar que arranca

En Portainer → **Containers** deben aparecer:

| Contenedor | Estado esperado |
|---|---|
| `phototag-db-1` | running (healthy) |
| `phototag-web-1` | running (healthy) — tarda hasta 1 minuto |
| `phototag-worker-1` | running |
| `phototag-cloudflared-1` | running |

- En los **logs** de `web` debe salir `event=superadmin_created username=admin` (solo la primera vez) y el arranque de Gunicorn.
- En los logs de `cloudflared`: `Registered tunnel connection` (cuatro veces).
- En Cloudflare Zero Trust → Tunnels, el túnel pasa a **Healthy**.

Ninguno de los contenedores publica puertos en el NAS; es lo correcto.

---

## 6. Primer uso

1. Abre `https://fotos.midominio.com` desde fuera de casa (por ejemplo, con el móvil en datos). Debe aparecer el candado de HTTPS y la pantalla de login.
2. Entra con `admin` y `SUPERADMIN_PASSWORD`.
3. **Más → Usuarios → Nuevo usuario** para cada familiar. Entrarán con `DEFAULT_USER_PASSWORD` y tendrán que elegir la suya.
4. **Instalar la app en el móvil**:
   - **Android (Chrome, Edge, Samsung Internet)**: al abrir la web aparece un aviso «Instala PhotoTag» → **Instalar**. Si no aparece: menú **⋮ → Instalar aplicación**.
   - **Android (Firefox)**: el aviso explica la ruta: menú **⋮ → Instalar**.
   - **iPhone / iPad (Safari)**: el aviso indica **Compartir → Añadir a pantalla de inicio**. iOS no permite instalar con un botón; hay que hacerlo desde ese menú.
   - «Ahora no» oculta el aviso 30 días en ese dispositivo.
5. **Prueba de escaneo**: deja un escaneo en `phototag/scans/inbox`. En menos de dos minutos aparecerán sus fotos en la galería y el original pasará a `scans/processed`.

---

## 7. Conectar Immich

Immich sigue en su propio stack; solo necesita leer la carpeta `export`:

1. En el stack de Immich, añade al servicio `immich-server` el volumen en solo lectura:

   ```yaml
   volumes:
     - /srv/dev-disk-by-uuid-1234abcd-…/phototag/export:/mnt/phototag:ro
   ```

2. Redespliega Immich.
3. Immich → **Administración → Bibliotecas externas → Crear**, ruta de importación `/mnt/phototag`.
4. Immich → **Administración → Ajustes → Ajustes de metadatos**: activa la **importación de caras desde los metadatos**.
5. Tras cada exportación de PhotoTag (**Administración → Exportación a Immich → Exportar ahora**), **reescanea** la biblioteca externa.

---

## 8. Actualizar PhotoTag

Cuando haya cambios en `main`:

1. Portainer → **Stacks → phototag → Pull and redeploy** (activa *Re-pull image* si aparece la opción) → **Update**.
2. Portainer descarga la nueva versión del repositorio y, gracias a `pull_policy: build` en el servicio `web`, **reconstruye la imagen** (las capas sin cambios salen de la caché). `web` aplica las migraciones al arrancar.
3. Comprueba que la imagen es nueva: Portainer → **Images** → `phototag:latest` → la fecha de creación debe ser la de ahora.

Si la fecha no cambia (algunas versiones de Portainer no reconstruyen al redesplegar), reconstruye por SSH y vuelve a desplegar:

```bash
docker build --target runtime -t phototag:latest \
  "https://<usuario>:<token>@github.com/<usuario>/phototag.git#main"
```

y después, en Portainer, **Stacks → phototag → Stop** y **Start** (o *Update the stack*).

Las variables de entorno se cambian en la misma pantalla del stack (**Environment variables**) y se aplican con **Update the stack**.

---

## 9. Copias de seguridad

- El `worker` hace un volcado diario de la base de datos a las 03:00 en `phototag/backups/` (`phototag-AAAAMMDD-HHMMSS.dump`) y conserva 14 días.
- Copia inmediata: Portainer → `phototag-worker-1` → **Console** → `python manage.py backup_db`.
- Incluye la carpeta compartida `phototag` en tu copia de seguridad de OMV (por ejemplo, con el plugin *rsnapshot* o *borgbackup* de omv-extras): `photos`, `scans/processed` y `backups` son lo importante.

Restaurar una copia (por SSH en el NAS):

```bash
docker exec -i phototag-db-1 pg_restore -U phototag -d phototag --clean --if-exists \
  < /srv/dev-disk-by-uuid-1234abcd-…/phototag/backups/phototag-AAAAMMDD-HHMMSS.dump
```

---

## 10. Problemas frecuentes

| Síntoma | Causa probable | Solución |
|---|---|---|
| El stack no se despliega: «variable is required» | Falta una variable obligatoria | Revisa el bloque del paso 4.5. |
| Error al clonar el repositorio | Token caducado o sin permiso *Contents: Read* | Genera otro token (paso 2) y actualiza la autenticación del stack. |
| `Bad Request (400)` en el navegador | El dominio no está en `DJANGO_ALLOWED_HOSTS` | Pon exactamente `fotos.midominio.com`, sin `https://` ni barra final. |
| `403 Forbidden (CSRF)` al iniciar sesión | `DJANGO_CSRF_TRUSTED_ORIGINS` incorrecto | Debe ser `https://fotos.midominio.com`. |
| Error 502 o 1033 de Cloudflare | El túnel no llega a `web` | Comprueba que la ruta pública es `HTTP` → `web:8000`, que `web` está *healthy* y que `cloudflared` tiene `Registered tunnel connection`. |
| `web` no pasa a *healthy* | La base de datos no arranca o hay un error de configuración | Mira los logs de `web` y `db`. |
| `Permission denied` en los logs del `worker` | Las carpetas no son de `PUID:PGID` | Repite el paso 1.5 con los valores de `id`. |
| Los escaneos no se procesan | El fichero aún se está copiando, o el formato no es JPG/PNG/TIFF | El worker espera a que el fichero lleve 5 s sin cambiar; revisa los logs del `worker`. |
| El móvil no ofrece instalar la app | No es HTTPS, Rocket Loader activo, o el aviso se descartó | Abre siempre `https://…`; desactiva Rocket Loader; en Android usa menú ⋮ → Instalar; en iPhone, Safari → Compartir. Para volver a ver el aviso, borra los datos del sitio. |
| La app instalada pide el código de Cloudflare a menudo | Sesión de Access corta | Sube *Session Duration* de la aplicación de Access (paso 3.3). |
| Las fotos exportadas no aparecen en Immich | La biblioteca externa no se ha reescaneado | Immich → Bibliotecas externas → *Scan*. |
