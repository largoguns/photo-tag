# Guía de despliegue: OpenMediaVault + Portainer + Tailscale Funnel

Esta guía instala PhotoTag en un NAS con **OpenMediaVault (OMV) sobre un PC x86_64**, con **Portainer** construyendo la imagen directamente desde el repositorio de GitHub, y la publica en internet por **HTTPS (puerto 443)** con **Tailscale Funnel**. No hace falta dominio propio ni abrir ningún puerto en el router (funciona también detrás de CGNAT): la conexión sale desde el NAS hacia Tailscale, que da una dirección fija y gratuita del estilo `https://phototag.tu-tailnet.ts.net` con certificado válido.

```
Móvil / navegador ──HTTPS 443──► Tailscale Funnel ──conexión cifrada──► tailscale ──► web:8000
                                                                       (en tu NAS, red Docker interna)
```

Los familiares **no necesitan cuenta de Tailscale** ni instalar nada: abren la dirección en el navegador y entran con su usuario de PhotoTag. Solo tú necesitas la cuenta, para dar de alta el NAS.

Tiempo estimado: 45–60 minutos la primera vez.

---

## 0. Requisitos

- OMV 7 con **omv-extras** y **Docker** (plugin *compose* de omv-extras) funcionando.
- **Portainer CE** instalado y accesible en tu red local.
- Una **cuenta de Tailscale** (plan *Personal*, gratuito). Puedes entrar con Google, Microsoft, GitHub o Apple.
- El repositorio `phototag` en **GitHub** (privado).
- Acceso SSH al NAS (para crear carpetas y comprobar permisos).

La dirección pública será `https://<TS_HOSTNAME>.<tu-tailnet>.ts.net`; en esta guía, `phototag.tu-tailnet.ts.net` (el nombre de tu *tailnet* lo verás en el paso 3).

---

## 1. Preparar las carpetas en OMV

PhotoTag necesita cuatro carpetas en el NAS:

| Carpeta | Contenido |
|---|---|
| `scans` | Escaneos (`inbox` donde los dejas, `processed` donde quedan los originales) |
| `photos` | Fotos recortadas y miniaturas |
| `export` | Fotos catalogadas con su XMP, para Immich |
| `backups` | Copias diarias de la base de datos |

En esta guía las carpetas viven en un pool **mergerfs** de OMV (varios discos unidos), en `/srv/mergerfs/storage/phototag`. Si usas un solo disco, la ruta será del estilo `/srv/dev-disk-by-uuid-1234abcd-…/phototag`; sustitúyela en todos los pasos.

1. Elige dónde irá la carpeta `phototag`: dentro del pool mergerfs, o en OMV **Almacenamiento → Carpetas compartidas → Crear** sobre el disco que prefieras (copia su **ruta absoluta**).
2. Por SSH, crea las subcarpetas:

   ```bash
   mkdir -p /srv/mergerfs/storage/phototag/{scans/inbox,scans/processed,photos,export,backups}
   ```

3. Averigua qué usuario de OMV será el propietario (el que usarás para copiar escaneos por SMB). Lista los usuarios reales del NAS:

   ```bash
   getent passwd | awk -F: '$3 >= 1000 && $3 < 65534 {print $1, "uid="$3, "gid="$4}'
   ```

   Anota `uid` → será `PUID` y `gid` → será `PGID` (en OMV suele ser `100`, el grupo `users`). No uses `root`. Si no sale ninguno, crea uno en OMV: **Usuarios → Usuarios → Crear**.

4. Asigna las carpetas a ese usuario (sustituye `1000:100` por tus valores):

   ```bash
   chown -R 1000:100 /srv/mergerfs/storage/phototag
   chmod -R u+rwX,g+rwX /srv/mergerfs/storage/phototag
   ls -ln /srv/mergerfs/storage/phototag   # todas deben mostrar tu uid:gid
   ```

5. Con mergerfs, ten en cuenta:
   - La base de datos **no** va al pool: PostgreSQL usa el volumen Docker `pgdata`, en el disco donde Docker guarda sus datos. Así debe quedarse (Postgres sobre FUSE es más lento y menos fiable). Sus copias diarias sí van a `backups`.
   - El pool debe estar montado **antes** de que arranque Docker (OMV lo hace por defecto). Si tras un reinicio los contenedores ven las carpetas vacías, reinicia el stack.

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

## 3. Tailscale Funnel

### 3.1 Preparar la cuenta

1. Crea la cuenta en [tailscale.com](https://tailscale.com) (plan *Personal*, gratuito).
2. Panel de administración → **DNS**:
   - Anota el **Tailnet name** (del estilo `tail1234.ts.net`, o elige uno más legible con *Rename tailnet*). La dirección de PhotoTag será `phototag.<tailnet-name>`.
   - Comprueba que **MagicDNS** está activado.
   - En **HTTPS Certificates**, pulsa **Enable HTTPS**.
3. **Access controls**: Funnel necesita el atributo `funnel` en la política. Si al activar Funnel (paso 5) la consola avisa de que no está permitido, sigue el enlace que muestra para añadirlo; o añádelo a mano al fichero de políticas:

   ```json
   "nodeAttrs": [
     { "target": ["autogroup:member"], "attr": ["funnel"] }
   ]
   ```

### 3.2 Clave de autenticación

1. **Settings → Keys → Generate auth key**.
2. Descripción `phototag-nas`. Deja **Reusable** y **Ephemeral** desactivados.
3. Genera y copia la clave (`tskey-auth-…`); no se vuelve a mostrar. Será `TS_AUTHKEY`.

La clave solo se usa en el primer arranque: después, la identidad del NAS queda guardada en el volumen `tailscale-state`.

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
   DJANGO_ALLOWED_HOSTS=phototag.tu-tailnet.ts.net
   DJANGO_CSRF_TRUSTED_ORIGINS=https://phototag.tu-tailnet.ts.net
   POSTGRES_PASSWORD=
   SUPERADMIN_USERNAME=admin
   SUPERADMIN_PASSWORD=
   DEFAULT_USER_PASSWORD=
   TS_AUTHKEY=
   TS_HOSTNAME=phototag
   TZ=Europe/Madrid
   VAPID_PUBLIC_KEY=
   VAPID_PRIVATE_KEY=
   VAPID_SUBJECT=mailto:tu-email@ejemplo.com
   PUID=1000
   PGID=100
   HOST_SCANS_DIR=/srv/mergerfs/storage/phototag/scans
   HOST_PHOTOS_DIR=/srv/mergerfs/storage/phototag/photos
   HOST_EXPORT_DIR=/srv/mergerfs/storage/phototag/export
   HOST_BACKUPS_DIR=/srv/mergerfs/storage/phototag/backups
   ```

   Para los secretos, genera valores aleatorios en el NAS por SSH:

   ```bash
   openssl rand -base64 48 | tr -d '\n=/+'; echo   # DJANGO_SECRET_KEY
   openssl rand -base64 24 | tr -d '\n=/+'; echo   # POSTGRES_PASSWORD
   ```

   | Variable | Qué poner |
   |---|---|
   | `DJANGO_SECRET_KEY` | Cadena aleatoria larga (comando de arriba). No la cambies después o se cerrarán todas las sesiones. |
   | `DJANGO_ALLOWED_HOSTS` | `<TS_HOSTNAME>.<tailnet-name>` (paso 3.1), **sin** `https://`. |
   | `DJANGO_CSRF_TRUSTED_ORIGINS` | La misma dirección **con** `https://`. |
   | `POSTGRES_PASSWORD` | Aleatoria. Solo se usa la primera vez que se crea la base de datos. |
   | `SUPERADMIN_PASSWORD` | Tu contraseña de administrador inicial (mín. 10 caracteres). Solo se usa en el primer arranque. |
   | `DEFAULT_USER_PASSWORD` | Contraseña genérica para los usuarios nuevos; tendrán que cambiarla al entrar. |
   | `TS_AUTHKEY` | La clave del paso 3.2. Tras el primer arranque puedes dejarla vacía. |
   | `TS_HOSTNAME` | Nombre del NAS en Tailscale y primera parte de la dirección (`phototag`). |
   | `PUID` / `PGID` | Los del paso 1.3. |
   | `VAPID_*` | Déjalas vacías en el primer despliegue; se configuran en el paso 6.6. |
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
| `phototag-tailscale-1` | running |

- En los **logs** de `web` debe salir `event=superadmin_created username=admin` (solo la primera vez) y el arranque de Gunicorn.
- En el panel de Tailscale → **Machines** aparece `phototag` como *Connected*.

Ninguno de los contenedores publica puertos en el NAS; es lo correcto.

### 5.1 Activar Funnel (solo la primera vez)

1. Portainer → `phototag-tailscale-1` → **Console** → *Connect* (`/bin/sh`), o por SSH `docker exec -it phototag-tailscale-1 sh`, y ejecuta:

   ```bash
   tailscale funnel --bg http://web:8000
   tailscale funnel status
   ```

   `status` debe mostrar `https://phototag.<tailnet-name> (Funnel on)` → `proxy http://web:8000`. Si dice que Funnel no está permitido, completa el paso 3.1.3 y repite.
2. La configuración se guarda en el volumen `tailscale-state`: sobrevive a reinicios y redespliegues.
3. Panel de Tailscale → **Machines** → `phototag` → **⋯ → Disable key expiry**. Si no, el NAS se desconecta a los 180 días y la web deja de estar accesible.
4. La primera vez, el DNS público de la dirección puede tardar hasta 10 minutos en funcionar.

---

## 6. Primer uso

1. Abre `https://phototag.tu-tailnet.ts.net` desde fuera de casa (por ejemplo, con el móvil en datos). Debe aparecer el candado de HTTPS y la pantalla de login.
2. Entra con `admin` y `SUPERADMIN_PASSWORD`.
3. **Más → Usuarios → Nuevo usuario** para cada familiar. Entrarán con `DEFAULT_USER_PASSWORD` y tendrán que elegir la suya.
4. **Instalar la app en el móvil**:
   - **Android (Chrome, Edge, Samsung Internet)**: al abrir la web aparece un aviso «Instala PhotoTag» → **Instalar**. Si no aparece: menú **⋮ → Instalar aplicación**.
   - **Android (Firefox)**: el aviso explica la ruta: menú **⋮ → Instalar**.
   - **iPhone / iPad (Safari)**: el aviso indica **Compartir → Añadir a pantalla de inicio**. iOS no permite instalar con un botón; hay que hacerlo desde ese menú.
   - «Ahora no» oculta el aviso 30 días en ese dispositivo.
5. **Prueba de escaneo**: deja un escaneo en `phototag/scans/inbox` o súbelo desde la app con el botón **Subir**. En menos de dos minutos aparecerán sus fotos en la galería y el original pasará a `scans/processed`. El contenedor `web` monta `scans/inbox` en escritura para recibir las subidas; por eso esa carpeta también debe ser de `PUID:PGID` (paso 1.5). El tamaño máximo por fichero se cambia con `UPLOAD_MAX_MB` (50 por defecto).
6. **Activar las notificaciones push** (opcional):
   1. Portainer → `phototag-web-1` → **Console** → *Connect* (`/bin/sh`) y ejecuta:

      ```bash
      python manage.py generate_vapid_keys
      ```

      Copia las dos líneas que imprime (`VAPID_PUBLIC_KEY=…` y `VAPID_PRIVATE_KEY=…`).
   2. **Stacks → phototag → Environment variables**: pega esos valores y pon en `VAPID_SUBJECT` un email tuyo con el prefijo `mailto:`. **Update the stack**.
   3. Genera las claves **una sola vez**: si las cambias, todos los dispositivos tendrán que volver a activar los avisos.
   4. En cada móvil: **Mi perfil → Avisos → Activar avisos en este dispositivo**, acepta el permiso y pulsa **Enviar aviso de prueba**.
      - **Android**: funciona desde Chrome aunque no instales la app.
      - **iPhone/iPad** (iOS 16.4 o posterior): primero instala la app (paso 6.4), ábrela desde el icono y actívalos desde ahí.
   5. Cada usuario elige qué avisos quiere en la misma tarjeta. Se envía como mucho un resumen por hora y tipo, y nada entre las 22:00 y las 9:00 (`NOTIFY_QUIET_HOURS`).

---

## 7. Conectar Immich

Immich sigue en su propio stack; solo necesita leer la carpeta `export`:

1. En el stack de Immich, añade al servicio `immich-server` el volumen en solo lectura:

   ```yaml
   volumes:
     - /srv/mergerfs/storage/phototag/export:/mnt/phototag:ro
   ```

2. Redespliega Immich.
3. Immich → **Administración → Bibliotecas externas → Crear**, ruta de importación `/mnt/phototag`.
4. Immich → **Administración → Ajustes → Ajustes de metadatos**: activa la **importación de caras desde los metadatos**.
5. Tras cada exportación de PhotoTag (**Administración → Exportación a Immich → Exportar ahora**), **reescanea** la biblioteca externa.

---

## 7b. Escanear desde PhotoTag (escáner de red)

Si tu impresora multifunción escanea por red con **eSCL/AirScan** (la HP DeskJet 2700 series lo hace), un administrador puede escanear desde **Administración → Escanear**: coloca las fotos en el cristal, pulsa **Escanear** y el NAS recibe la imagen y la procesa. Funciona desde el móvil junto a la impresora; tu PC no interviene.

1. **IP fija para la impresora**: en el router, reserva por DHCP la IP actual de la impresora (por ejemplo `192.168.18.46`) para que no cambie.
2. **Comprueba que responde**: abre en un navegador de tu red `http://192.168.18.46/eSCL/ScannerCapabilities`. Debe mostrar un XML con `DeskJet 2700`. La primera petición puede tardar o fallar si la impresora estaba en reposo: recarga. Si no responde nunca, activa **Webscan** en el panel web de la impresora (la misma IP en el navegador).
3. **Variable en Portainer**: `SCANNER_URL=http://192.168.18.46/eSCL` → **Update the stack**.
4. **Diagnóstico**: Portainer → `phototag-worker-1` → **Console** → `python manage.py scanner_check`. Debe listar `airscan:e0:PhotoTag` y sus resoluciones, y terminar con `Scanner reachable`.
5. Ya aparece **Escanear** en Administración y en el menú del avatar de los administradores.

Los escaneos se guardan en PNG sin pérdida (unos 25–45 MB por A4 a 600 ppp) en `scans/processed`, como cualquier escaneo.

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

**Al estrenar la gamificación** (niveles y ranking), ejecuta una vez en la consola del contenedor `worker` el cálculo de puntos de lo que la familia ya ha hecho:

```bash
python manage.py rebuild_scores
```

Recalcula todo desde el historial y no envía avisos. Se puede repetir sin riesgo: siempre da el mismo resultado.

---

## 9. Copias de seguridad

- El `worker` hace un volcado diario de la base de datos a las 03:00 en `phototag/backups/` (`phototag-AAAAMMDD-HHMMSS.dump`) y conserva 14 días.
- Copia inmediata: Portainer → `phototag-worker-1` → **Console** → `python manage.py backup_db`.
- El volumen Docker `tailscale-state` guarda la identidad del NAS en Tailscale y la configuración de Funnel. Si se pierde, repite los pasos 3.2 y 5.1 (con una clave nueva).
- Incluye la carpeta compartida `phototag` en tu copia de seguridad de OMV (por ejemplo, con el plugin *rsnapshot* o *borgbackup* de omv-extras): `photos`, `scans/processed` y `backups` son lo importante.

Restaurar una copia (por SSH en el NAS):

```bash
docker exec -i phototag-db-1 pg_restore -U phototag -d phototag --clean --if-exists \
  < /srv/mergerfs/storage/phototag/backups/phototag-AAAAMMDD-HHMMSS.dump
```

---

## 10. Problemas frecuentes

| Síntoma | Causa probable | Solución |
|---|---|---|
| El stack no se despliega: «variable is required» | Falta una variable obligatoria | Revisa el bloque del paso 4.5. |
| Error al clonar el repositorio | Token caducado o sin permiso *Contents: Read* | Genera otro token (paso 2) y actualiza la autenticación del stack. |
| `Bad Request (400)` en el navegador | La dirección no está en `DJANGO_ALLOWED_HOSTS` | Pon exactamente `phototag.tu-tailnet.ts.net`, sin `https://` ni barra final. |
| `403 Forbidden (CSRF)` al iniciar sesión | `DJANGO_CSRF_TRUSTED_ORIGINS` incorrecto | Debe ser `https://phototag.tu-tailnet.ts.net`. |
| La dirección no carga (no resuelve o no conecta) | Funnel no está activo, o el DNS aún no se ha propagado | `tailscale funnel status` en el contenedor `tailscale` (paso 5.1); espera 10 minutos la primera vez. |
| «Funnel not available» o «not allowed» | Falta el atributo `funnel` o los certificados HTTPS | Completa el paso 3.1 y repite el 5.1. |
| Error 502 al abrir la web | Funnel no llega a `web` | Comprueba que `web` está *healthy* y que el destino de Funnel es exactamente `http://web:8000`. |
| `phototag` aparece *Expired* en Tailscale | Caducó la clave del nodo | **Machines → phototag → Reauthenticate** (o una clave nueva en `TS_AUTHKEY` y redesplegar) y desactiva *key expiry* (paso 5.1.3). |
| Tras redesplegar aparece otra máquina `phototag-1` | Se perdió el volumen `tailscale-state` | Borra la máquina antigua en Tailscale, renómbrala a `phototag` y repite el paso 5.1. |
| `web` no pasa a *healthy* | La base de datos no arranca o hay un error de configuración | Mira los logs de `web` y `db`. |
| `Permission denied` en los logs del `worker` | Las carpetas no son de `PUID:PGID` | Repite el paso 1.4 con los valores del paso 1.3. |
| Los escaneos no se procesan | El fichero aún se está copiando, o el formato no es JPG/PNG/TIFF | El worker espera a que el fichero lleve 5 s sin cambiar; revisa los logs del `worker`. |
| «Este navegador no admite avisos» | Navegador sin Web Push o iPhone sin la app instalada | En iPhone, instala la app y actívalos desde ella. En Android, usa Chrome. |
| La prueba de avisos falla | Claves VAPID vacías o mal copiadas, o el `worker`/`web` sin salida a internet | Revisa las variables `VAPID_*` (sin espacios ni comillas) y los logs de `worker` (`event=push_failed`). |
| No llegan los resúmenes | Horas de silencio, intervalo de una hora o el tipo desactivado | Es lo esperado: comprueba las casillas de la tarjeta «Avisos» y `NOTIFY_QUIET_HOURS`. |
| «La impresora no responde» al escanear | Impresora apagada, en reposo profundo o fuera de la WiFi | Enciéndela, comprueba el icono de WiFi y que `http://IP/eSCL/ScannerCapabilities` responde; ejecuta `scanner_check` en el worker. |
| «No se encuentra el escáner» | `SCANNER_URL` mal escrita o la IP cambió | Revisa la reserva de IP en el router y la variable (debe terminar en `/eSCL`). |
| El móvil no ofrece instalar la app | No es HTTPS, o el aviso se descartó | Abre siempre `https://…`; en Android usa menú ⋮ → Instalar; en iPhone, Safari → Compartir. Para volver a ver el aviso, borra los datos del sitio. |
| Las fotos exportadas no aparecen en Immich | La biblioteca externa no se ha reescaneado | Immich → Bibliotecas externas → *Scan*. |
