# PhotoTag — Especificación técnica

> Documento de especificación para implementación por un agente de IA.
> Idioma de la interfaz: español (principal) e inglés (ver 5.2). Idioma del código, identificadores, commits y comentarios: inglés.
> Este documento define **qué** construir. La forma de trabajar (comandos, convenciones y commits) está en `AGENTS.md`.
> Repositorio: `phototag` (GitHub, privado).

## 1. Contexto y objetivo

Se dispone de cientos de escaneos A4 de fotografías familiares en papel. Cada escaneo contiene entre 3 y 4 fotografías colocadas sobre el cristal del escáner.

PhotoTag es **una única aplicación** (all-in-one) que:

1. Separa automáticamente cada escaneo en fotografías individuales y detecta las caras de cada una.
2. Ofrece una web multiusuario donde varios familiares identifican **quién sale** en cada foto y **de qué evento es** (cumpleaños, boda, comunión, Navidad…), mediante un sistema de **propuestas y votaciones**.
3. Exporta las fotos con la catalogación consensuada como metadatos estándar (XMP) para que **Immich** las importe ya con las caras etiquetadas.

## 2. Arquitectura general

Un único proyecto Django y una única imagen Docker. La aplicación se organiza en apps internas de Django:

| App Django | Responsabilidad |
|---|---|
| `scanner` | Recorte de escaneos y detección de caras |
| `catalog` | Fotos, caras, personas, eventos, propuestas y votaciones |
| `accounts` | Gestión de usuarios, roles y autenticación |
| `exporter` | Generación de XMP y exportación para Immich |

```
 /data/scans/inbox ──► [worker: scanner] ──► /data/photos + BD
                                                   │
 Internet ─► Tailscale Funnel ─► [tailscale] ─► [web] ◄┴► [db: PostgreSQL]
                                                   │
                              [worker: exporter] ──► /data/export ──► Immich (biblioteca externa)
```

Servicios del stack (Docker Compose, desplegado con Portainer en el NAS):

| Servicio | Imagen | Función |
|---|---|---|
| `web` | `phototag` (propia) | Servidor web (Gunicorn) |
| `worker` | `phototag` (misma imagen) | Tareas en segundo plano: procesado de escaneos, miniaturas y exportación |
| `db` | `postgres:16-alpine` | Base de datos |
| `tailscale` | `tailscale/tailscale` | Expone `web` a internet por HTTPS con Tailscale Funnel |

`web` y `worker` usan la misma imagen con distinto comando de arranque. Immich ya está desplegado en el NAS en otro stack y **no forma parte de este**; solo comparte el volumen `/data/export` en modo lectura.

## 3. Recorte de imágenes (app `scanner`)

### 3.1 Entrada

- Carpeta `/data/scans/inbox` con escaneos A4 (JPG, PNG o TIFF), resolución típica 300–600 ppp.
- **Subidas desde la web**: cualquier usuario puede subir imágenes (pantalla «Subir»). Al subir elige el tipo: **escaneo con varias fotos** (se recorta como cualquier escaneo) o **foto suelta** (se usa la imagen entera, respetando la orientación EXIF, y solo se detectan caras). Se valida el tipo real (JPEG/PNG/TIFF) y el tamaño (`UPLOAD_MAX_MB`, 50 por defecto; hasta 20 ficheros por envío), se rechazan los duplicados por SHA-256 y el fichero se escribe en `inbox` de forma atómica. Cada subida queda registrada (`Upload`) con su estado (en cola, procesada, duplicada o con error) y el escaneo resultante guarda quién lo subió.
- Fondo blanco o casi blanco (tapa del escáner). Las fotos pueden estar ligeramente giradas y muy próximas, aunque normalmente no se tocan.

### 3.2 Procesamiento

Para cada escaneo:

1. Calcular el hash SHA-256 del fichero. Si ya existe un `Scan` con ese hash, saltarlo. El proceso debe ser **idempotente**.
2. Convertir a escala de grises, desenfoque gaussiano ligero y umbralización (Otsu o adaptativa) para separar fotos del fondo.
3. Operaciones morfológicas (cierre) para rellenar huecos dentro de cada foto.
4. Detectar contornos externos (`cv2.findContours`) y descartar los de área menor que `MIN_PHOTO_AREA_RATIO` (por defecto, 3 % del A4).
5. Para cada contorno válido, obtener el rectángulo mínimo rotado (`cv2.minAreaRect`) y aplicar transformación de perspectiva para obtener la foto recta.
5b. **Fotos pegadas**: si un rectángulo es demasiado alargado para ser una sola copia (proporción mayor de 1,7), se busca en su zona central una línea recta que lo cruce (un borde continuo de al menos el 40 % del ancho y 4 veces más largo que los típicos de la imagen). Si existe, se parte por ella; en copias con marco se corta en el centro, entre los bordes interiores de ambos marcos. Se repite hasta dos niveles y el escaneo va a revisión («fotos pegadas separadas automáticamente»).
6. Recortar un margen interior configurable (`CROP_INSET_PX`, por defecto 4 px) para eliminar restos del fondo.
6b. **Orientación**: para detectar fotos colocadas de lado o boca abajo, se prueban las cuatro orientaciones (0°, 90°, 180° y 270°) con YuNet sobre una copia reducida. La foto se gira si una orientación distinta de 0° suma una confianza en caras ≥ `ORIENTATION_MIN_SCORE` (0.8) y supera a la siguiente en un factor ≥ `ORIENTATION_MARGIN` (1.5). Si gana sin margen suficiente, no se gira y el escaneo va a revisión. Las fotos sin caras no se tocan. El giro aplicado se guarda en `Photo.rotation`.
6c. **Posibles duplicados**: se calcula un hash perceptual (dHash de 64 bits, ignorando imágenes sin detalle) y se compara, en las cuatro orientaciones, con el de las fotos existentes. Con una distancia de Hamming ≤ `DUPLICATE_MAX_DISTANCE` (6), la foto se marca como posible duplicado de la más antigua (`Photo.duplicate_of`) y el escaneo va a revisión. El comando `python manage.py find_duplicates` lo aplica a las fotos ya procesadas.
7. Detectar caras con **OpenCV YuNet** (`cv2.FaceDetectorYN`, modelo `face_detection_yunet_2023mar.onnx` incluido en la imagen) sobre cada foto. Umbral configurable (`FACE_SCORE_THRESHOLD`, por defecto 0.7).
8. Guardar cada foto como JPG de calidad 95 en `/data/photos/`, generar miniaturas (400 px y 1600 px) en `/data/photos/thumbs/` y crear en BD los registros `Scan`, `Photo` y `Face` (source `auto`) dentro de una transacción.
9. Mover el escaneo original a `/data/scans/processed/` (nunca borrarlo).

### 3.3 Casos dudosos

Marcar el `Scan` con `needs_review = True` cuando:

- Se detecten menos de 1 o más de 6 fotos.
- Algún contorno tenga una relación de aspecto anómala (mayor de 4:1).
- Dos rectángulos se solapen.
- Se hayan separado fotos pegadas (5b).
- La orientación de alguna foto sea dudosa (6b).
- Alguna foto sea un posible duplicado (6c). En la cola se ven las dos fotos juntas y se puede descartar la copia o marcar «No es duplicado».

Los escaneos marcados se procesan igualmente y aparecen en la cola de revisión del administrador (ver 5.6).

### 3.4 Ejecución

- Comando `python manage.py process_scans` que procesa todo lo pendiente en `inbox`.
- Opciones: `--watch` (bucle cada `POLL_INTERVAL_SECONDS`, por defecto 60) y `--force <hash|fichero>` para reprocesar un escaneo. Reprocesar no debe borrar votos ni propuestas de fotos que sigan existiendo.
- Un administrador también puede **reprocesar desde la web** (cola de revisión o panel «Escaneo de origen» de la vista de foto): se marca `Scan.reprocess_requested` y el `worker` lo ejecuta en segundos con el algoritmo actual, conservando votos y recortes manuales. Las fotos que dejan de aparecer se borran (también su copia exportada).
- El servicio `worker` ejecuta `process_scans --watch` y el planificador de tareas (ver 5.8).
- Logs estructurados por stdout (visibles en Portainer).
- Dependencias: `opencv-python-headless`, `numpy`. Sin GPU.

### 3.5 Sugerencias de identidad

El sistema **sugiere** quién puede ser una cara, pero **nunca asigna** una identidad: toda identidad se acepta mediante propuesta y votación (5.4).

- Modelo **OpenCV SFace** (`cv2.FaceRecognizerSF`, `face_recognition_sface_2021dec.onnx` incluido en la imagen), complementario de YuNet. Sin GPU ni dependencias nuevas.
- Para cada cara (automática o dibujada a mano) el `worker` calcula una huella de 128 valores normalizada: recorta la cara con margen, obtiene los puntos de referencia con YuNet para alinearla y, si no los encuentra, usa el recuadro redimensionado.
- Las huellas se calculan al procesar el escaneo y, para las caras nuevas o sin huella, en una tarea periódica (`EMBEDDING_POLL_SECONDS`, por defecto 60). Al reprocesar un escaneo se recalculan. Comando `python manage.py compute_embeddings [--all]`.
- Similitud coseno contra las caras con persona aceptada; la puntuación de cada persona es la de su cara más parecida. Se sugiere a partir de `FACE_MATCH_THRESHOLD` (por defecto 0.363) y se marca como «muy probable» a partir de `FACE_MATCH_STRONG` (por defecto 0.5).
- No se sugieren personas ya aceptadas en otra cara de la misma foto ni personas que ya tengan una propuesta abierta para esa cara.

### 3.6 Ajuste manual del recorte

Cuando el recorte automático no acierta (bordes de anchura variable, esquinas redondeadas, fotos cortadas o algo torcidas), un **administrador** puede corregirlo foto a foto con el botón «Recortar» de la vista de foto.

- **Editor**: muestra la foto recta y girada como se ve, sobre el **escaneo original** que la rodea, con un recuadro de 4 bordes y 4 esquinas arrastrables (también hacia fuera, para recuperar lo que se cortó) y un deslizador **Enderezar** de ±10° (pasos de 0,1°). Al enderezar, el recuadro se reduce lo justo para no salirse del escaneo. No se permite un recuadro fuera del escaneo ni menor de 64 px.
- **Vista previa del escaneo**: el `worker` guarda una copia reducida (2000 px de lado mayor) en `/data/photos/previews/<sha256>.jpg` al procesar, y genera las que falten de escaneos antiguos en una tarea periódica. Se sirve solo a administradores con sesión, nunca como estático público. En las subidas de una sola foto el escaneo es la propia foto: solo se puede recortar hacia dentro.
- **Aplicación**: la web solo registra el recorte pedido (`Photo.pending_crop`, en píxeles del escaneo); el `worker` lo aplica en unos segundos desde el original: rehace la foto y sus miniaturas, traslada las caras con intervención humana (persona, dibujadas o con propuestas), vuelve a detectar las automáticas, recalcula huellas y phash y marca la foto para reexportar. Mientras tanto, la vista de foto muestra «Aplicando el recorte…».
- **Zoom** en ambos modos: rueda del ratón (acerca hacia el puntero), pellizcar con dos dedos o botones +/−/ajustar; con zoom se desplaza la vista arrastrando fuera del recuadro o de las esquinas.
- **Modo Perspectiva** (fotos de una copia en papel hechas con el móvil): se muestra la imagen original entera, girada como la foto, con 4 esquinas libres que se llevan a las de la copia; una lupa ampliada ayuda a afinar al arrastrar. Al guardar, la foto se aplana con una transformación de perspectiva (`Photo.crop_quad`). El cuadrilátero debe ser convexo, estar dentro de la imagen y tener lados de al menos 64 px. Una foto con recorte en perspectiva solo se edita en este modo.
- **Detección de esquinas**: en las subidas de una sola foto el modo Perspectiva arranca con las esquinas de la copia detectadas automáticamente (bordes y contornos de 4 vértices con OpenCV sobre la vista previa), y el botón «Detectar esquinas» lo repite en cualquier foto. Es solo una sugerencia: nunca se recorta nada sin que un administrador lo guarde.
- El recorte queda marcado como manual (`Photo.crop_manual`) y **se respeta al reprocesar** el escaneo con `--force`.
- Queda en la auditoría (`photo.crop`).

## 4. Usuarios y autenticación (app `accounts`)

### 4.1 Autenticación

- Login local con **usuario y contraseña**. Sin federación (OAuth, SSO, Google, etc.).
- Las contraseñas se almacenan con el hasher por defecto de Django; nunca en claro.
- **Contraseña inicial genérica**: al crear un usuario, el superadmin le asigna una contraseña inicial (puede ser la misma para todos, configurable con `DEFAULT_USER_PASSWORD`). En el primer inicio de sesión el sistema **obliga a cambiarla** antes de acceder a cualquier otra pantalla (campo `must_change_password`).
- Al restablecer la contraseña de un usuario, vuelve a la contraseña genérica y se reactiva `must_change_password`.
- Cierre de sesión manual y caducidad de sesión configurable (`SESSION_COOKIE_AGE`, por defecto 30 días).

### 4.2 Roles

| Rol | Permisos |
|---|---|
| **Superadmin** | Todo lo del administrador + gestión de usuarios (incluidos administradores) |
| **Administrador** | Tipos de evento, umbrales de votación, cola de revisión, fusión de personas, decisiones forzadas, exportación |
| **Colaborador** | Ver fotos, proponer y votar |

### 4.3 Superadmin

- Se crea automáticamente en el primer arranque a partir de las variables `SUPERADMIN_USERNAME` y `SUPERADMIN_PASSWORD` si no existe ningún superadmin. Si ya existe, las variables se ignoran.
- Debe existir siempre al menos un superadmin activo: la aplicación impide desactivarlo, degradarlo o borrarlo si es el último.

### 4.4 Módulo de gestión de usuarios

Accesible solo para el superadmin, dentro de la propia interfaz de PhotoTag (no usar el admin de Django para esto):

- Listado de usuarios con búsqueda, rol, estado (activo/inactivo) y fecha del último acceso.
- Crear usuario: nombre de usuario, nombre a mostrar, rol. Se le asigna la contraseña genérica.
- Editar nombre a mostrar y rol.
- Desactivar y reactivar usuarios (no se borran, para conservar su historial de propuestas y votos).
- Restablecer contraseña (vuelve a la genérica, ver 4.1).
- Opcional: vincular un usuario a una `Person` del catálogo («este usuario soy yo en las fotos»).

Todos los usuarios pueden cambiar su propia contraseña y su nombre a mostrar desde «Mi perfil».

## 5. Catalogación y votación (app `catalog`)

### 5.1 Stack

- Python 3.12, Django 5.x, HTMX, PostgreSQL 16.
- Gunicorn + WhiteNoise para estáticos.
- Pillow para miniaturas; `pyexiftool` (con `exiftool` instalado en la imagen) para escribir XMP.
- Sin framework JS pesado. Solo JS mínimo propio para dibujar y seleccionar recuadros de cara.

### 5.2 Diseño responsive-by-design

La web debe funcionar igual de bien en escritorio y en móvil. Es un requisito de diseño desde el inicio, no una adaptación posterior:

- **Mobile-first**: los estilos base son para móvil y se amplían con media queries para tablet (≥ 768 px) y escritorio (≥ 1024 px).
- Maquetación con CSS Grid y Flexbox; sin anchos fijos en píxeles para contenedores. Se puede usar un framework CSS ligero (por ejemplo, Pico CSS o Tailwind compilado en el build) o CSS propio.
- **Vista de foto en móvil**: imagen a ancho completo arriba y paneles de caras, evento y fecha debajo, plegables. **En escritorio**: imagen a la izquierda y paneles en una columna lateral a la derecha.
- Interacción táctil de primera clase: usar Pointer Events para seleccionar y dibujar recuadros de cara, de modo que funcione igual con ratón y con dedo. Nada debe depender solo de `hover`.
- Objetivos táctiles de al menos 44 × 44 px (botones de voto, recuadros de cara pequeños con área ampliada).
- **Material Design 3 (Material You)**: paleta tonal clara y oscura, botones en píldora, chips e iconos Material Symbols Rounded (sprite SVG propio, sin CDN). Barra superior con la marca y el **avatar del usuario arriba a la derecha**, que abre su menú (perfil, administración, usuarios, idioma y cerrar sesión).
- Navegación: **barra de navegación flotante** en móvil (se oculta al bajar y reaparece al subir) y **navigation rail** a la izquierda desde 840 px. Botón flotante (FAB) «Subir» en Inicio y Galería.
- **Sin desbordes**: los botones nunca parten su texto en varias líneas (se recortan con puntos suspensivos y conservan el texto completo como etiqueta accesible) y la barra de herramientas de la foto pasa a solo iconos cuando no cabe.
- **Zoom**: la aplicación no permite pellizcar para hacer zoom en la página. En la vista de foto, al tocar la imagen se abre un **visor a pantalla completa** con la imagen original y zoom propio (pellizco, doble toque, rueda y botones, hasta ×8), con los recuadros de cara ocultos por defecto y un interruptor para mostrarlos.
- Al abrir la **app instalada** sin avisos activados en ese dispositivo, se propone activarlos con un banner (se puede posponer 30 días).
- Imágenes con `srcset` usando las miniaturas de 400 y 1600 px para no descargar originales en móvil.
- Gestos opcionales en móvil: deslizar a izquierda/derecha para pasar de foto.
- Accesibilidad básica: contraste suficiente (AA en ambos temas), etiquetas en formularios, navegación por teclado en escritorio.
- **Tema claro y oscuro**: por defecto sigue la preferencia del sistema (`prefers-color-scheme`); cada usuario puede forzar «Claro» u «Oscuro» en «Mi perfil» (campo `theme` del usuario). Los recuadros de cara sobre la foto mantienen sus colores en ambos temas.
- **Idiomas**: español (principal) e inglés con la internacionalización de Django. El idioma se detecta del navegador (`Accept-Language`) y se puede cambiar con un selector Español/English (menú «Más», «Mi perfil» y pantalla de login) que se guarda en una cookie del dispositivo. Los textos fuente están en español y se traducen en `locale/en`. Los tipos de evento tienen un nombre en inglés opcional (`EventType.name_en`); los metadatos exportados (sección 6) siempre se escriben en español.
- **App instalable (PWA)**: manifest (`/manifest.webmanifest`), iconos (incluido *maskable* y `apple-touch-icon`) y service worker en `/sw.js`. Al abrir la web en un móvil se detecta el navegador por su *user agent* y se propone instalarla: botón «Instalar» donde el navegador lo permite (`beforeinstallprompt`, Chromium en Android) e instrucciones en iOS/iPadOS («Compartir → Añadir a pantalla de inicio») y en otros navegadores Android. El aviso no aparece si ya está instalada y se puede posponer 30 días. Requiere HTTPS (Tailscale Funnel). El service worker solo cachea estáticos y una página sin conexión; nunca fotos ni páginas con datos.
- **Confirmaciones**: las acciones destructivas o con efecto directo (decisiones de administrador, «No es una cara», restablecer contraseña, desactivar usuario, descartar recorte, fusionar personas) piden confirmación con un diálogo modal propio de la aplicación (`<dialog>`), nunca con el diálogo nativo del navegador. En móvil se muestra como hoja inferior.

### 5.3 Modelo de datos

| Entidad | Campos principales |
|---|---|
| `Scan` | hash, fichero, fecha de procesado, needs_review, reviewed, subido_por (nullable), ancho y alto del original, reprocesado pedido |
| `FileCleanup` | tipo (`photo`, `export`, `preview`, `original`), nombre o ruta, fecha. Tareas de ficheros pendientes tras un borrado |
| `Upload` | usuario, nombre original, nombre guardado, tipo (`scan`/`single`), sha256, tamaño, estado (`pending`, `processed`, `duplicate`, `failed`), scan (nullable) |
| `Photo` | scan, fichero, índice, ancho, alto, ángulo, estado (`pending`, `in_progress`, `cataloged`), exportada_en, giro aplicado, giro pendiente, rectángulo de recorte, cuadrilátero de perspectiva (nullable), recorte pendiente (nullable), recorte manual, versión de imagen, phash, posible_duplicado_de (nullable), ubicación aceptada (nullable) |
| `Person` | nombre a mostrar, honorífico (opcional), notas, creada_por, merged_into (nullable), user vinculado (nullable) |
| `PersonNickname` | person, texto, texto normalizado, fecha. Único por (person, texto normalizado). Ver 5.13 |
| `PersonDetailProposal` | person, campo (`honorific`/`nickname`), acción (`set`/`add`/`remove`), texto, nickname (nullable, para `remove`), autor, fecha, estado (`open`, `accepted`, `rejected`, `superseded`), decidida_en, decidida_por. Ver 5.13 |
| `PersonDetailVote` | proposal, usuario, valor (+1 / −1), fecha. Único por (proposal, usuario) |
| `Location` | nombre, nombre normalizado (único), activa, provisional, propuesta_por (nullable), merged_into (nullable), fecha. Catálogo de lugares en texto («Casa de Los Tarantos»), sin geoposición por ahora |
| `Face` | photo, x, y, w, h, score, source (`auto`/`manual`), creada_por, person aceptada (nullable), embedding (huella SFace, nullable) |
| `EventType` | nombre, nombre en inglés, activo, orden, provisional, propuesto_por (nullable). Lista estándar editable por los administradores: Cumpleaños, Boda, Comunión, Bautizo, Navidad, Vacaciones… `provisional` marca los escritos con «Otro…» por la familia, pendientes de revisión del administrador (5.5) |
| `Proposal` | photo, face (nullable), kind, valor, autor, fecha, estado (`open`, `accepted`, `rejected`, `superseded`) |
| `Vote` | proposal, usuario, valor (+1 / −1), fecha. Único por (proposal, usuario) |
| `ScoreEvent` | usuario, motivo, puntos, objeto (tipo e id), fecha. Único por (usuario, motivo, objeto) |
| `Achievement` | usuario, código del logro, fecha. Único por (usuario, código) |
| `Relationship` | person_from, person_to, kind, actividad (texto, opcional), propuesta aceptada, fecha. Única por (person_from, person_to, kind). Ver 5.12 |
| `RelationshipProposal` | person_from, person_to, kind, actividad, acción (`add`/`remove`), relationship (nullable, para `remove`), autor, fecha, estado (`open`, `accepted`, `rejected`, `superseded`), decidida_en, decidida_por |
| `RelationshipVote` | proposal, usuario, valor (+1 / −1), fecha. Único por (proposal, usuario) |

Coordenadas de las caras en píxeles de la foto recortada (esquina superior izquierda + ancho y alto).

Tipos de propuesta (`kind`):

- `face_identity`: asigna una `Person` (existente o nueva) a una `Face`.
- `event`: tipo de evento + descripción libre opcional («Boda de Luis y Ana»).
- `date`: fecha aproximada con precisión variable (año, década o fecha exacta).
- `location`: ubicación de la foto, una `Location` existente o nueva.
- `add_face`: nueva cara dibujada manualmente (el detector no la encontró).
- `remove_face`: la cara detectada es un falso positivo.

Al proponer un nombre que no existe, se crea una `Person` provisional; si la propuesta se rechaza y la persona no tiene otras referencias, se elimina.

### 5.4 Reglas de votación

- Crear una propuesta cuenta como un voto +1 del autor.
- Una propuesta se **acepta** cuando los votos a favor son ≥ `VOTE_MIN_APPROVALS` (por defecto 2) **y** la proporción a favor es ≥ `VOTE_APPROVAL_RATIO` (por defecto 0.66).
- Se **rechaza** cuando los votos en contra son ≥ `VOTE_MIN_APPROVALS` y superan a los votos a favor.
- Pueden coexistir varias propuestas para el mismo campo (por ejemplo, dos nombres distintos para la misma cara). Al aceptarse una, el resto pasan a `superseded`.
- Un valor aceptado puede reabrirse con una nueva propuesta; el anterior sigue vigente hasta que la nueva se acepte.
- Un administrador puede aceptar o rechazar directamente cualquier propuesta (queda registrado).
- Los umbrales son configurables desde la administración.
- **Tipos de evento estándar**: el tipo de evento se elige de una lista estándar (vacaciones, Navidad, cumpleaños, bautizo, comunión…). Si no está, se elige **«Otro…»** y se escribe («Graduación»). Si el texto coincide con un tipo existente (sin distinguir tildes ni mayúsculas, en los dos idiomas) se usa ese; si no, se crea un tipo **provisional** y la propuesta queda **«En revisión»**. Los lugares van en Ubicación.
- **Revisión del administrador** (Administración → Tipos de evento y Ubicaciones, sección «Pendientes de revisión»; la portada de Administración muestra cuántos hay): los eventos escritos con «Otro…» y los lugares nuevos **no se votan**; solo los decide un administrador. Mientras tanto la propuesta se ve en la foto con la insignia «En revisión» y sin botones de voto, no cuenta como «pendiente de mi voto» y la entrada no se ofrece al proponer. Por cada entrada, el administrador puede:
  - **aceptarla** tal cual o **con un texto más adecuado**: se renombra y, si el texto coincide con una entrada existente, se fusiona con ella (fotos y propuestas pasan a la existente). Sus propuestas abiertas se aceptan;
  - **rechazarla**: sus propuestas se rechazan, las fotos que la tuvieran la pierden y la entrada se borra;
  - en eventos, **«Es un lugar»**: pasa a ser una ubicación aceptada; sus fotos la toman si no tenían otra y sus propuestas pasan a ser de ubicación, aceptadas.
  Todo queda en la auditoría. Lo que escribe un administrador se acepta al momento y entra directamente en la lista. Al estrenar la revisión, los tipos que la familia había añadido antes quedan pendientes de revisión (sus fotos los conservan hasta que se decida) y «Otro» deja de ofrecerse.
- **Los administradores no proponen**: lo que guarda un administrador o el superadmin en una foto (nombre de una cara, evento, fecha, ubicación, cara dibujada o «no es una cara») se acepta al momento, como propuesta decidida por él; los botones dicen «Guardar».
- En la vista de foto, los paneles de Evento, Fecha y Ubicación empiezan **plegados**; el que se acaba de usar queda abierto para ver su resultado o su error.
- **Ubicaciones nuevas**: si el texto coincide con una `Location` existente (sin distinguir tildes ni mayúsculas), se usa esa y la propuesta se vota; si no, se crea una **provisional** con el texto recortado y las mayúsculas tal como se escribieron, y va a la revisión del administrador, igual que los eventos nuevos.
- Una foto pasa a `cataloged` cuando todas sus caras tienen persona aceptada y tiene evento aceptado. La fecha y la ubicación no son obligatorias.

### 5.5 Pantallas

1. **Login** y **cambio obligatorio de contraseña** en el primer acceso.
2. **Inicio**: progreso global (fotos catalogadas / total), propuestas abiertas pendientes de mi voto y botón «Siguiente foto sin catalogar».
3. **Galería** al estilo Google Photos: cuadrícula justificada que respeta la proporción de cada foto, con **secciones por año** de la fecha aceptada (más reciente primero; las fechadas solo por década en «Década de 19X0» y las demás en «Sin fecha» al final). Filtros como **chips** (estado, persona, evento, ubicación, década y «pendientes de mi voto») que abren hojas inferiores con las opciones. La de ubicación tiene buscador y muestra el número de fotos de cada una (`?location=<id>`; las ubicaciones fusionadas se resuelven a la de destino).
   - **Varias personas**: la hoja de persona (con buscador y caras) permite elegir varias y muestra las fotos donde **salen todas juntas**; el chip dice «Ana +2» y la URL (`?person=12&person=45`) se puede compartir. Las personas fusionadas se resuelven a la persona destino.
   - **Carga por bloques** (como Google Fotos e Immich), para que miles de fotos no saturen la memoria: la página trae solo el esqueleto (cada sección partida en bloques de hasta 120 fotos, con su recuento y la suma de proporciones para estimar su altura) y los primeros bloques ya llenos. El navegador pide cada bloque (`?block=i`) al acercarse a menos de ~2 pantallas y lo vacía al pasar de ~4, dejando un hueco de su altura medida; así nunca hay más de unos pocos bloques vivos. Lo visible no se mueve cuando un bloque cambia de altura, y al volver desde una foto se reabre en el mismo punto (`?anchor=i`).
   - **Barra de años** a la derecha: arrastrar recorre toda la galería y tocar un año salta a él; en escritorio, al pasar el ratón, muestra los años con el actual resaltado; en móvil aparece al hacer scroll con una burbuja del año. Los años también son enlaces, accesibles con teclado.
   - **Búsquedas laxas**: todos los buscadores (personas, usuarios, autocompletado de nombres y hoja de persona de la galería) ignoran tildes y mayúsculas y buscan cada palabra por separado en cualquier orden: «ari» encuentra «María» y «lop mar», «María López». Las personas se encuentran también por su honorífico y sus apodos (5.13).
   - **Nombres de persona**: se normalizan al guardarlos (mayúscula inicial en cada palabra salvo las partículas de, del, la, las, los, y, e) y se reutiliza la persona existente aunque cambien tildes o mayúsculas. El campo de nombre sugiere personas existentes con su cara. En toda la interfaz se muestra el **nombre visible**: honorífico + nombre si hay honorífico («Tito Paco») y, si no, el nombre a mostrar (5.13).
   - **Subir**: elección del tipo, selección o arrastre de ficheros, progreso y lista «Mis subidas».
   - **Zoom de la galería**: pellizcar (o Ctrl + rueda en escritorio) cambia el tamaño de las filas, como en Google Photos; cada dispositivo recuerda el tamaño.
   - **Compartir**: en la galería, el modo selección (botón «Seleccionar» o pulsación larga) muestra la barra «N seleccionadas · Compartir · Descargar». En la vista de foto, un botón «Compartir». Se usa la hoja de compartir del dispositivo con los ficheros originales (Web Share API); sin ella, se descarga el JPG o un ZIP (máximo `SHARE_MAX_PHOTOS`, 20). Compartir y descargar quedan en la auditoría.
   - **Escanear** (solo administradores, si `SCANNER_URL` está configurada): «¿Todo listo para escanear?», resolución 300/600 ppp (600 por defecto) y botón «Escanear». El `worker` escanea por red con SANE (`sane-airscan`, protocolo eSCL/AirScan, p. ej. HP DeskJet 2700), guarda un PNG sin pérdida en `inbox` y registra un `Upload` de tipo escaneo, de modo que el procesado habitual lo recorta y lo atribuye. La pantalla muestra el progreso, el resultado (miniaturas de los recortes) y vuelve a preguntar. Un solo escaneo a la vez (`ScanJob`); errores comprensibles (impresora apagada, ocupada, no encontrada). Diagnóstico: `python manage.py scanner_check`.
   - **Girar**: cualquier usuario puede girar una foto 90° desde su vista. El giro lo aplica el `worker` en unos segundos (fichero, miniaturas y recuadros de cara) y se mantiene al reprocesar el escaneo.
   - **Recortar** (solo administradores): editor de recorte sobre el escaneo original (ver 3.6).
4. **Vista de foto** (pantalla principal):
   - Imagen con recuadros de caras numerados superpuestos. Al pulsar un recuadro se abre su panel.
   - Por cada cara: persona aceptada o propuestas abiertas con botones 👍 / 👎, y opción de proponer otro nombre (autocompletado de personas existentes).
   - Panel de evento, fecha y ubicación con el mismo mecanismo. El campo de ubicación sugiere las del catálogo con la búsqueda laxa.
   - Botón para dibujar una cara nueva y para marcar una cara como falso positivo.
   - Por cada cara sin persona aceptada: hasta 3 sugerencias automáticas («¿Es Abuela Carmen? · muy probable»). Pulsar una crea una propuesta normal con el voto del usuario.
   - Navegación anterior/siguiente y «siguiente foto sin catalogar».
5. **Personas**: listado con miniatura de cara y número de fotos; ficha de persona con todas sus fotos y una sección «Puede que aparezca en» con las caras sin identificar más parecidas, cada una con un botón para proponerla. En la ficha, tocar el avatar cambia la foto principal; los administradores pueden **cambiar el nombre** (lápiz junto al nombre; se normaliza y no puede coincidir con otra persona, para eso está «Fusionar»; sus fotos se reexportan) y tienen «Fusionar» en el menú «⋮». La ficha incluye la sección «Familia y relaciones» y el botón «Ver árbol» (ver 5.12), y el honorífico y la sección «Apodos» (ver 5.13).
6. **Mi perfil**: cambiar contraseña, nombre a mostrar, tema, idioma y avisos. En móvil es una lista de ajustes plegables; en ventanas anchas (≥ 840 px), dos columnas: la tarjeta del usuario con un índice fijo de secciones a la izquierda y todas las secciones abiertas como tarjetas a la derecha.
7. **Administración**: tipos de evento, ubicaciones, umbrales, cola de revisión de escaneos, fusión de personas y exportación.
   - **Ubicaciones**: listado con número de fotos y quién propuso cada una. Los administradores pueden **renombrar**, **fusionar** (las fotos y propuestas pasan a la de destino) y **desactivar** (deja de sugerirse). Renombrar o fusionar reexporta las fotos afectadas. Todo queda en la auditoría.
8. **Gestión de usuarios** (solo superadmin, ver 4.4).
9. **Árbol genealógico**: destino propio en la barra de navegación y el rail, y accesible desde la ficha de persona (ver 5.12).

### 5.6 Cola de revisión

El administrador ve los escaneos con `needs_review` junto a sus recortes y puede marcarlos como correctos o descartar recortes erróneos. Un recorte concreto se corrige con el editor de recorte (3.6); para rehacer todo el escaneo, el botón «Reprocesar» (o `process_scans --force`).

**Borrar** (solo administradores, con confirmación): una foto (desde la cola o la vista de foto) o un escaneo entero con todas sus fotos (cola o panel «Escaneo de origen»). Se eliminan caras, propuestas, votos y las personas, tipos de evento o ubicaciones provisionales que solo existían por ellas. Si la foto se exportó, se borran también su JPG y su `.xmp` de la exportación, para que Immich la retire. Al borrar un escaneo su original se aparta en `scans/deleted/` (nunca se borra), y ese fichero o un reescaneo se pueden volver a procesar. La web solo cambia la base de datos; los ficheros los limpia el `worker` desde una cola (`FileCleanup`). Todo queda en la auditoría.

### 5.7 Auditoría

Toda creación de propuesta (también de relación, 5.12), voto, decisión de administrador, fusión de personas y acción de gestión de usuarios queda registrada con usuario y fecha (tabla `AuditLog`).

**Pantalla «Auditoría»** (`/accounts/audit/`, solo superadmin; enlazada desde Usuarios con un botón «Actividad» por usuario, desde la ficha de cada usuario y desde Administración): línea de tiempo de todo lo registrado, filtrable con chips por **usuario**, **evento** (uno concreto o una categoría entera: Catalogación, Fotos, Escaneos y subidas, Personas, Usuarios y accesos, Avisos, Administración) y **periodo** (hoy, 7 días, 30 días, este año). Cada entrada muestra un texto legible, un enlace al objeto si sigue existiendo (o su nombre con «ya no existe») y sus detalles. Incluye los inicios de sesión y los intentos fallidos registrados por django-axes (`AXES_ENABLE_ACCESS_FAILURE_LOG`). Filtrando por un usuario aparece su resumen (propuestas por estado, votos a favor y en contra, subidas, escaneos, acciones e inicios de sesión) y las pestañas Historial, Propuestas y Votos. Toda acción que se registre en el código debe tener su etiqueta en el catálogo de eventos (lo comprueba un test).

### 5.8 Tareas en segundo plano

Se ejecutan en el servicio `worker`. Basta con un planificador sencillo (por ejemplo, `django-q2` con PostgreSQL como broker, o APScheduler) para no añadir Redis al stack.

### 5.9 Notificaciones push (app `notifications`)

Avisos Web Push (estándar VAPID) en los dispositivos que cada usuario active desde «Mi perfil». En Android funcionan desde el navegador; en iOS/iPadOS ≥ 16.4 solo con la app instalada (5.2) y activándolos desde un botón dentro de ella.

- Tres tipos, activables por usuario: **fotos nuevas para catalogar**, **propuestas pendientes de mi voto** (de otros autores y no votadas) y **mis propuestas aceptadas o rechazadas**.
- **Resumen agrupado**: el `worker` revisa cada `NOTIFY_POLL_MINUTES` (10) lo ocurrido desde el último aviso de cada tipo y envía como mucho uno por tipo cada `NOTIFY_MIN_INTERVAL_MINUTES` (60). En las horas de silencio `NOTIFY_QUIET_HOURS` (`22-9`, zona `TZ`) no se envía nada; lo acumulado se avisa al terminar.
- **Eventos y lugares nuevos para revisar** (solo administradores, preferencia propia): mismo resumen agrupado; abre Administración → Tipos de evento (o Ubicaciones si solo hay lugares).
- Al activar los avisos no se notifica el histórico anterior.
- Textos genéricos, sin nombres de personas, en el idioma con el que se suscribió cada dispositivo. Al tocar el aviso se abre la pantalla correspondiente (siguiente foto sin catalogar, galería «pendientes de mi voto» o, para las propuestas resueltas, Inicio).
- **Escaneos terminados** (preferencia «Mis escaneos terminados», activada por defecto): quien pulsa «Escanear» recibe un aviso **inmediato**, sin agrupar ni horas de silencio, cuando el escaneo ya está procesado («4 fotos listas del escaneo de las 13:10», que abre la primera foto), si era un duplicado o si falló (con el motivo, abre la pantalla de escaneo). No se muestra si la pantalla de escaneo está a la vista, porque ya enseña el resultado.
- Las suscripciones caducadas (respuesta 404/410 del servicio push) se eliminan. Sin claves VAPID configuradas la función queda desactivada.
- Envío con `pywebpush`. Las claves se generan con `python manage.py generate_vapid_keys`.

### 5.10 Gamificación (app `gamification`)

Para animar a la familia a subir, proponer y votar. **Solo participan los colaboradores**; el superadmin (y el rol administrador, que se deprecará) no puntúan.

- **Puntos por resultados**, nunca por clics (tabla `ScoreEvent`, única por usuario, motivo y objeto: nada puntúa dos veces):

| Motivo | Puntos |
|---|---|
| Foto subida y procesada (no si es posible duplicado; se retira si se descarta o se borra) | 5 |
| Identidad aceptada (10 si venía de una sugerencia automática) | 20 |
| Fecha, evento o ubicación aceptados | 10 |
| Cara nueva aceptada | 15 |
| Falso positivo aceptado | 5 |
| Relación aceptada (añadir o quitar, 5.12) | 10 |
| Honorífico o apodo aceptado (poner, añadir o quitar, 5.13) | 5 |
| Primera identidad aceptada de una persona en todo el álbum | +15 |
| Voto que coincide con el resultado (tope de 20 puntos de votos al día) | 2 |

  No cuenta el +1 automático del autor. Rechazar no resta, pero baja el **acierto** (aceptadas / cerradas). Si una propuesta aceptada la sustituye después otra, sus puntos se retiran; al borrar una foto se retiran los de la foto, sus propuestas y sus votos. Un fallo al puntuar nunca interrumpe la votación ni el procesado.
- **Niveles**: Curioso (0), Aprendiz de archivero (50), Archivero (150), Detective de familia (400), Cronista (900), Memoria de la familia (1800) y Guardián del álbum (3500).
- **Logros** (`Achievement`, se anuncian una vez): Primera subida, Primera identificación, Detective (10/50/100 identidades), Cronista (10/50 fechas), Ojo de halcón (5 falsos positivos), Rescatista (25 fotos), Desempate (tu voto decide una propuesta), Genealogista (10/50 relaciones aceptadas) y Racha (7 días seguidos proponiendo o votando).
- **Avisos**: push inmediato al subir de nivel o conseguir un logro (preferencia «Mis niveles y logros»).
- **Retos para subir de nivel** (preferencia propia, activada por defecto): a un colaborador que lleva 2 días sin proponer ni votar se le envía, como mucho cada `NOTIFY_CHALLENGE_DAYS` (3) días y fuera de las horas de silencio, el reto que más le motive, sin nombres de personas: que alguien le ha adelantado en el ranking de la semana (a cuántos puntos está), o cuántos puntos le faltan para el siguiente nivel y cuántas propuestas esperan su voto o cuántas fotos quedan por catalogar.
- **Cinturón**: el avatar de cada colaborador (menú, perfil y ranking) lleva un anillo con el color de su nivel, como los cinturones de un arte marcial (blanco, amarillo, naranja, verde, azul, marrón y negro), que se va llenando en el sentido de las agujas del reloj con el color del nivel siguiente según su progreso.
- **Ranking** (`/ranking/`, visible para todos): esta semana, este mes y total, con podio de los tres primeros. El detalle de puntos de cada colaborador solo lo ve el superadmin (pestaña «Puntos» de la auditoría).
- **Inicio**: tarjeta «Tu nivel» con el progreso y, para todos, la meta colectiva con mensajes al 25, 50, 75 y 100 % del álbum catalogado. **Mi perfil**: «Tu progreso» con nivel, acierto y logros.
- `python manage.py rebuild_scores` recalcula todo desde el historial, sin avisos (al estrenar la función).

### 5.11 Identificarse como persona

Un usuario puede decir «esta persona del álbum soy yo» con el botón **«Soy yo»** de la ficha de la persona (inmediato, queda en la auditoría; el superadmin puede deshacerlo desde la ficha del usuario). Una persona ya vinculada a otro usuario no se puede elegir. Vinculado, en «Mi perfil → Tú en el álbum» elige cualquiera de sus caras como **foto de perfil** (`User.avatar_face`), que solo cambia su avatar (menú, ranking, auditoría), no la foto principal de la persona. Si esa cara deja de ser suya, se usa la foto principal de la persona.

### 5.12 Árbol genealógico y relaciones

Las personas del álbum se relacionan entre sí con el mismo mecanismo de propuesta y votación que los nombres. Con esas relaciones se dibujan dos árboles: el **familiar** y el de **relaciones** (todo lo que no es familia).

**Tipos de relación.** Es un catálogo fijo en el código y no se edita desde la administración. Las etiquetas son **neutras**: no hay género en las personas y se usa el genérico.

| Árbol | Tipo (directo ↔ inverso) | Simétrica |
|---|---|---|
| Familiar | Progenitor ↔ Hijo | no |
| Familiar | Abuelo ↔ Nieto | no |
| Familiar | Tío ↔ Sobrino | no |
| Familiar | Suegro ↔ Hijo político | no |
| Familiar | Hermano, Primo, Cuñado | sí |
| Familiar | Pareja, Expareja | sí |
| Relaciones | Padrino ↔ Ahijado | no |
| Relaciones | Amigo, Compadre, Vecino, Compañero de trabajo, Compañero de clase | sí |
| Relaciones | Compañero de actividad (con la actividad como texto libre: «fútbol», «coro») | sí |

**Reciprocidad.** Cada relación se guarda una sola vez, en **forma canónica**:
- Las asimétricas van en sentido descendente: progenitor→hijo, abuelo→nieto, tío→sobrino, suegro→hijo político, padrino→ahijado. Proponer «X es hijo de Y» guarda `parent(Y→X)`.
- Las simétricas van con `person_from.id < person_to.id`.

Desde cada persona se muestra con la etiqueta que le corresponde: si A es progenitor de B, la ficha de B dice «A · Progenitor» y la de A, «B · Hijo». La forma canónica se garantiza con restricciones de base de datos.

**Propuesta y votación** (mismas reglas que 5.4):
- Crear una propuesta cuenta como voto +1 del autor. Se aplican los umbrales `VOTE_MIN_APPROVALS` y `VOTE_APPROVAL_RATIO` y la decisión directa del administrador.
- Por cada par de personas hay como mucho **una relación familiar** aceptada. Las propuestas familiares abiertas para el mismo par compiten entre sí; al aceptarse una, el resto pasan a `superseded`. Una relación familiar aceptada se **reabre** con una propuesta de otro tipo y sigue vigente hasta que esa nueva se acepta. Por ejemplo, Pareja → Expareja.
- Las relaciones sociales se suman: un par puede ser primo, compañero de trabajo y vecino a la vez, con una relación de cada tipo como mucho.
- Una relación aceptada se elimina con una propuesta de **«Quitar relación»**, que también se vota.
- Si la otra persona no existe, se crea como `Person` provisional, igual que al proponer un nombre. Así entran familiares sin fotos, como los bisabuelos. Si la propuesta se rechaza y la persona no tiene otras referencias (caras, propuestas o relaciones), se elimina.

**Validaciones al proponer**, con mensajes comprensibles:
- No se puede relacionar a una persona consigo misma.
- No se puede duplicar una relación aceptada ni una propuesta abierta igual.
- Una persona no puede tener más de dos progenitores aceptados.
- No se pueden crear ciclos de ascendencia (progenitor y abuelo cuentan como ascendencia).
- No se puede contradecir una relación familiar deducida, como proponer «Primo» cuando ya se deduce «Hermano».

**Relaciones deducidas.** Las relaciones primarias son Progenitor/Hijo y Pareja. A partir de ellas, y de los hermanos, primos y tíos explícitos, se deducen sin votación hermanos, abuelos, tíos, sobrinos, primos, suegros y cuñados. Se muestran como «deducida». Si alguien propone una relación que ya se deduce, no se crea y se le avisa. Reglas:
- Hermanos: progenitor común o relación explícita, de forma transitiva entre los explícitos (A hermano de B y B de C ⇒ A de C); los medios hermanos no se encadenan.
- Los hermanos comparten tíos y primos: A hermano de B y A primo de C ⇒ B y C primos; un tío explícito de A lo es también de B.
- Tíos: los hermanos de mis progenitores y los **progenitores de mis primos** (C hijo de D y C primo de A ⇒ D tío de A y de sus hermanos). Primos: los hijos de mis tíos y los hermanos de mis primos. Sobrinos, el inverso de tíos.
- No existen «primo segundo» ni «tío abuelo»: el primo de mi primo no es mi primo (salvo que sea su hermano) y nada se deduce más allá.

Todos los tipos se pueden proponer de forma explícita aunque falte el eslabón intermedio, por ejemplo un suegro sin fotos de la pareja. En el árbol se dibujan como **enlace directo**, con línea discontinua.

**Ficha de persona.** Sección «Familia y relaciones» con:
- Las relaciones aceptadas, agrupadas por árbol.
- Las deducidas.
- Las propuestas abiertas, con 👍 / 👎.
- El botón «Añadir relación». Abre una hoja inferior para elegir el tipo y buscar a la persona con sus caras, usando la búsqueda laxa de 5.5. En «Compañero de actividad» se pide además la actividad.
- El botón **«Ver árbol»**, que abre el árbol centrado en esa persona.

**Pantalla «Árbol genealógico»** (`/tree/`). Es un destino propio en la barra de navegación y el rail, con el icono `family_history` en el sprite. Arriba tiene un selector segmentado **Familiar | Relaciones**.
- **Familiar**: árbol generacional centrado en una persona, con los antepasados arriba y los descendientes abajo.
  - Los hermanos van en la misma fila y las parejas, una al lado de la otra: la pareja con línea continua y la expareja con línea discontinua gris.
  - Los hijos cuelgan de la línea que une a sus dos progenitores, con soporte para varias parejas y medio hermanos.
  - Cada nodo muestra el avatar de la persona y su nombre.
  - Filtros en chips:
    - Persona central: por defecto la vinculada con «Soy yo» (5.11); si no hay ninguna, se pide elegir.
    - Generaciones hacia arriba y hacia abajo: de 1 a 5, por defecto 3.
    - Tipos (parejas y exparejas, familia política, enlaces directos).
    - «Mostrar propuestas abiertas», que se dibujan atenuadas y discontinuas.
- **Relaciones**: red centrada en una persona, con las relaciones directas en un primer anillo y, opcionalmente, las de segundo grado en un segundo anillo. Las líneas se distinguen por tipo. Filtros: tipo de relación, actividad concreta y profundidad (1 o 2).
- Al tocar un nodo se abre una hoja inferior (panel lateral desde 840 px) con «Centrar aquí», «Ver ficha» y «Añadir relación».
- El lienzo se mueve y se amplía con arrastre, pellizco, rueda y botones (Pointer Events), y tiene un botón «Centrar». Tiene su propio desplazamiento, así que la página nunca hace scroll horizontal, ni siquiera a 360 px.
- Hay una alternativa accesible, «Ver como lista»: la misma información en una lista indentada que se recorre con el teclado.

**Integración con el resto de la aplicación:**
- Las propuestas de relación cuentan como «pendientes de mi voto» en Inicio, que enlaza a la ficha de la persona, y en los avisos push (5.9). Si solo hay propuestas de relación pendientes, el aviso abre Inicio. Los textos siguen sin nombres de personas.
- Se auditan la creación, el voto, la decisión y el borrado de relaciones, con eventos en la categoría «Personas».
- Gamificación (5.10): las relaciones aceptadas puntúan, cuenta el voto que coincide con el resultado y hay un logro nuevo, Genealogista. Si una relación aceptada se sustituye o se quita, sus puntos se retiran. `rebuild_scores` las incluye.
- **Fusionar personas** pasa las relaciones a la persona destino. Se descartan las relaciones de la persona consigo misma, se juntan los duplicados y las propuestas que entren en conflicto pasan a `superseded`.
- Las relaciones no se exportan a Immich (ver 11).

**Implementación:**
- Toda la lógica va en servicios testeables:
  - `catalog/services/relationships.py`: proponer, validar, votar y aceptar. Reutiliza `tally`/`outcome` de la votación y bloquea las dos personas con `select_for_update`.
  - `catalog/services/kinship.py`: deducciones, etiquetas y detección de ciclos. Carga el grafo en memoria con pocas consultas.
  - `catalog/services/tree_layout.py`: coordenadas de nodos y aristas. El árbol familiar se dispone en reloj de arena por generaciones, compactando los subárboles para que no se solapen. La red de relaciones es radial, por anillos y sin simulación de fuerzas.
- Solo la tabla `Relationship` alimenta el árbol, la ficha y las deducciones.
- **Sin dependencias nuevas**: el servidor dibuja el árbol como SVG en línea desde una plantilla, con los avatares que sirven las vistas autenticadas de siempre. Los filtros recargan el SVG con HTMX y el movimiento y el zoom reutilizan el JS propio del visor a pantalla completa.
- Límites: 5 generaciones en cada sentido y profundidad 2 en la red. Objetivo: menos de 300 ms en el servidor con 1 000 personas.
- `scripts/agents/seed_demo.py` genera una familia de demo de 3–4 generaciones con parejas, una expareja y relaciones sociales.

### 5.13 Honorífico y apodos

Cada persona puede tener un **honorífico** y varios **apodos**. Los dos se proponen y se votan como el resto de la catalogación.

- **Honorífico**: un tratamiento opcional que va delante del nombre («Tito», «Tita», «Abuela»). Hay uno como mucho. Se guarda con mayúscula inicial («tito» → «Tito»).
- **Apodos**: una lista, que puede estar vacía, de motes («Pico pata», «Pico Paco»). Se recortan y se guardan tal cual. No hay dos apodos iguales en la misma persona (sin distinguir tildes ni mayúsculas), pero sí pueden repetirse entre personas distintas.
- **Nombre visible**: honorífico + nombre a mostrar («Tito» + «Paco» = «Tito Paco»), o solo el nombre a mostrar si no hay honorífico. Se usa en toda la interfaz: recuadros y sugerencias de cara, chips, listado y ficha de personas, árbol, ranking y auditoría. Cambiar el nombre a mostrar (5.5) sigue siendo cosa de administradores y no pasa por votación.
- **Búsqueda**: la búsqueda laxa de 5.5 también busca por el honorífico y los apodos. «tito paco» y «pico pata» encuentran a la misma persona. Si el resultado aparece por un apodo, la sugerencia lo indica («Tito Paco · Pico pata»).

**Ficha de persona.** El nombre visible va como título, con un lápiz para proponer otro honorífico o quitarlo. La sección **«Apodos»** muestra los apodos aceptados (cada uno con «Quitar»), las propuestas abiertas con 👍 / 👎 y el botón «Añadir apodo». Los apodos solo se ven en la ficha.

**Propuesta y votación** (mismas reglas que 5.4: el +1 del autor, `VOTE_MIN_APPROVALS`, `VOTE_APPROVAL_RATIO` y la decisión directa del administrador). Puede proponer cualquier colaborador.
- **Honorífico** (`set`, con texto vacío para quitarlo): las propuestas abiertas de una misma persona compiten entre sí. Al aceptarse una, el resto pasan a `superseded`. El valor vigente se mantiene hasta que se acepte el nuevo.
- **Apodos**: `add` añade un apodo y `remove` quita uno aceptado. Cada apodo se vota por separado y las propuestas de apodos distintos no se sustituyen entre sí. No se puede proponer un apodo que ya existe ni duplicar una propuesta abierta igual: en ese caso se lleva al usuario a votar la que ya hay.
- Solo se pueden proponer para personas activas, ni provisionales ni fusionadas.

**Integración con el resto de la aplicación:**
- Las propuestas cuentan como «pendientes de mi voto» en Inicio, que enlaza a la ficha de la persona, y en los avisos push (5.9), con las mismas reglas que las relaciones (5.12).
- Se auditan la creación, el voto y la decisión, con eventos en la categoría «Personas».
- Gamificación (5.10): las propuestas aceptadas puntúan y cuenta el voto que coincide con el resultado. Si un honorífico aceptado se sustituye, sus puntos se retiran. `rebuild_scores` las incluye.
- **Fusionar personas**: se conserva el honorífico de la persona destino (o el de la absorbida si la destino no tiene) y se juntan los apodos de las dos sin duplicados. Las propuestas abiertas pasan a la persona destino, y las que queden en conflicto o duplicadas pasan a `superseded`.
- Exportación (6): `mwg-rs:Name` sigue siendo el nombre a mostrar, sin honorífico. Cambiar el honorífico o los apodos no reexporta nada.

**Implementación:**
- La lógica va en `catalog/services/person_details.py` (proponer, validar, votar, aceptar y normalizar). Reutiliza `tally`/`outcome` de la votación y bloquea la persona con `select_for_update`.
- El nombre visible se calcula en un único sitio del modelo (`Person.shown_name`) y lo usan todas las plantillas, los presentadores, las notificaciones y la auditoría.
- Sin dependencias nuevas.

## 6. Exportación a Immich (app `exporter`)

- Tarea manual (botón en administración) y opcionalmente periódica.
- Exporta solo fotos en estado `cataloged` cuya catalogación haya cambiado desde la última exportación.
- Por cada foto escribe en `/data/export/<año o "sin-fecha">/`:
  - La imagen JPG.
  - Un sidecar `<fichero>.jpg.xmp` con:
    - **Regiones de cara** en formato MWG (`mwg-rs:Regions`), con `mwg-rs:Name` = nombre a mostrar de la persona (sin honorífico, 5.13) y `mwg-rs:Type` = `Face`. Coordenadas **normalizadas (0–1) y centradas** (`stArea:x` y `stArea:y` son el centro), junto con `mwg-rs:AppliedToDimensions`.
    - `dc:subject` y `digiKam:TagsList` con el evento (por ejemplo, `Eventos/Boda`) y la descripción libre.
    - La ubicación aceptada en `Iptc4xmpCore:Location` y como etiqueta `Ubicaciones/<nombre>` en `dc:subject` y `digiKam:TagsList`. Aceptar otra ubicación, renombrarla o fusionarla cuenta como cambio de catalogación.
    - `exif:DateTimeOriginal` cuando la fecha aceptada tenga precisión suficiente. Si es solo un año, usar el 1 de enero de ese año y añadir la etiqueta `Fecha/aproximada`.
- Immich debe tener `/data/export` montado como **biblioteca externa** y activada la importación de caras desde metadatos (Administración → Ajustes de metadatos). Debe documentarse en el README.
- Limitación conocida de Immich: las fotos con caras en metadatos no pasan por su detección facial automática. Es el comportamiento deseado.

## 7. Seguridad (exposición pública vía Tailscale Funnel)

- Solo `web` es alcanzable desde `tailscale`. `db` y `worker` no publican puertos y están en una red Docker interna. El `worker` tiene además una red `egress` solo de salida para entregar las notificaciones push; `db` no tiene salida.
- `web` tampoco publica puertos en el host: `tailscale` accede por la red interna de Docker. No se abren puertos en el router.
- Configuración Django detrás de proxy: `SECURE_PROXY_SSL_HEADER`, `CSRF_TRUSTED_ORIGINS`, `ALLOWED_HOSTS` con el dominio público, cookies `Secure` y `HttpOnly`, `DEBUG=False`.
- Limitar intentos de login (`django-axes`: bloqueo tras 5 fallos durante 30 minutos).
- Validadores de contraseña de Django activos para la contraseña definitiva que elige cada usuario (longitud mínima 10).
- Sin registro abierto: solo el superadmin crea usuarios.
- Las imágenes se sirven a través de una vista Django que exige sesión, nunca como estáticos públicos.
- Todos los secretos (`DJANGO_SECRET_KEY`, contraseña de BD, `SUPERADMIN_PASSWORD`, `DEFAULT_USER_PASSWORD`, clave de autenticación de Tailscale) por variables de entorno en Portainer, nunca en el repositorio.
- La dirección de Funnel es pública y no hay una segunda capa de autenticación delante: la protección es el login de PhotoTag con `django-axes`. La IP del cliente para `django-axes` se toma de `X-Forwarded-For`, que Tailscale sobrescribe.

## 8. Despliegue

### 8.1 Volúmenes

| Ruta en contenedor | Uso | Montado en |
|---|---|---|
| `/data/scans` | Escaneos (`inbox`, `processed`, `deleted`) | worker (rw); web solo `inbox` (rw, para las subidas) |
| `/data/photos` | Recortes, miniaturas y vistas previas de los escaneos (`previews`) | web (ro), worker (rw) |
| `/data/export` | Salida para Immich | worker (rw); Immich lo monta como biblioteca externa (ro) |
| `/data/backups` | Copias de la BD | worker (rw) |
| `pgdata` | Datos de PostgreSQL | db |

Las rutas reales en el host se definen con variables (`HOST_SCANS_DIR`, `HOST_PHOTOS_DIR`, `HOST_EXPORT_DIR`, `HOST_BACKUPS_DIR`).

### 8.2 Variables de entorno

| Variable | Servicio | Ejemplo / defecto |
|---|---|---|
| `DJANGO_SECRET_KEY` | web, worker | — (obligatoria) |
| `DJANGO_ALLOWED_HOSTS` | web | `phototag.tu-tailnet.ts.net` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | web | `https://phototag.tu-tailnet.ts.net` |
| `DATABASE_URL` | web, worker | `postgres://phototag:***@db:5432/phototag` |
| `POSTGRES_PASSWORD` | db | — (obligatoria) |
| `SUPERADMIN_USERNAME` | web | `admin` |
| `SUPERADMIN_PASSWORD` | web | — (obligatoria en el primer arranque) |
| `DEFAULT_USER_PASSWORD` | web | — (obligatoria) |
| `UPLOAD_MAX_MB` | web | `50` |
| `ORIENTATION_MIN_SCORE` / `ORIENTATION_MARGIN` | worker | `0.8` / `1.5` |
| `DUPLICATE_MAX_DISTANCE` | worker | `6` |
| `ROTATION_POLL_SECONDS` | worker | `5` |
| `SHARE_MAX_PHOTOS` | web | `20` |
| `SCANNER_URL` | web, worker | `http://192.168.1.50/eSCL` (vacía = sin escaneo) |
| `SCAN_TIMEOUT_SECONDS` | worker | `300` |
| `TS_AUTHKEY` | tailscale | — (solo en el primer arranque) |
| `TS_HOSTNAME` | tailscale | `phototag` |
| `POLL_INTERVAL_SECONDS` | worker | `60` |
| `MIN_PHOTO_AREA_RATIO` | worker | `0.03` |
| `FACE_SCORE_THRESHOLD` | worker | `0.7` |
| `FACE_MATCH_THRESHOLD` | web | `0.363` |
| `EMBEDDING_POLL_SECONDS` | worker | `60` |
| `TZ` | todos | `Europe/Madrid` |
| `PUID` / `PGID` | web, worker | `1000` / `100` (propietario de las carpetas del NAS) |
| `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` | web, worker | — (vacías = sin notificaciones; ver 5.9) |
| `VAPID_SUBJECT` | web, worker | `mailto:admin@midominio.com` |
| `NOTIFY_POLL_MINUTES` / `NOTIFY_MIN_INTERVAL_MINUTES` / `NOTIFY_QUIET_HOURS` | worker | `10` / `60` / `22-9` |
| `NOTIFY_CHALLENGE_DAYS` | worker | `3` (días mínimos entre retos para subir de nivel) |

### 8.3 Requisitos del stack

- `docker-compose.yml` compatible con Portainer como stack desde repositorio Git: `web` construye la imagen en cada despliegue (`pull_policy: build`) y `worker` la reutiliza. Guía completa en `docs/deployment.md`.
- Dockerfile multi-stage; usuario no root; incluye `exiftool` y el modelo YuNet.
- `healthcheck` en `db` y `web`; `web` y `worker` dependen de `db` sano.
- `web` ejecuta migraciones, `collectstatic` y la creación del superadmin al arrancar.
- Copia de seguridad: `pg_dump` diario a `/data/backups` con retención de 14 días, programado en `worker`.

## 9. Estructura del repositorio

```
phototag/
├── AGENTS.md            (forma de trabajar del agente: comandos, convenciones, commits)
├── CLAUDE.md            (importa AGENTS.md para Claude Code)
├── docs/
│   └── specification.md (este documento)
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── .gitignore           (excluye .env, data/ e imágenes salvo las de tests)
├── README.md
├── pyproject.toml
├── manage.py
├── models/face_detection_yunet_2023mar.onnx
├── models/face_recognition_sface_2021dec.onnx
├── phototag/            (settings, urls, wsgi)
├── accounts/
├── catalog/
├── scanner/
├── exporter/
├── templates/
├── static/
└── tests/
```

## 10. Calidad y criterios de aceptación

- Tests unitarios del recorte con escaneos sintéticos generados en los tests (rectángulos de colores girados sobre fondo blanco): detectar el número correcto de fotos y su ángulo con tolerancia de ±2°.
- Tests de las reglas de votación: aceptación, rechazo, propuestas concurrentes, reapertura y decisión de administrador.
- Tests de relaciones (5.12): forma canónica y reciprocidad de etiquetas, votación (aceptar, rechazar, sustituir, quitar y decisión de administrador), cada validación, deducciones, fusión de personas, limpieza de provisionales y disposición del árbol sin solapes (varias parejas, exparejas y medio hermanos).
- Tests de gestión de usuarios: cambio obligatorio de contraseña, restablecimiento, desactivación y protección del último superadmin.
- Test de exportación que verifique con `exiftool` que el XMP generado contiene las regiones MWG con coordenadas normalizadas correctas.
- `ruff` para estilo y linting.
- Criterios de aceptación de extremo a extremo:
  1. Dejar un escaneo con 4 fotos en `inbox` produce 4 fotos visibles en la web en menos de 2 minutos.
  2. Dos usuarios votando a favor del mismo nombre para una cara la dejan aceptada.
  3. Una foto catalogada y exportada aparece en Immich con las personas etiquetadas tras reescanear la biblioteca externa.
  4. Un usuario no autenticado no puede acceder a ninguna página ni imagen.
  5. Un usuario nuevo no puede usar la aplicación hasta cambiar la contraseña genérica.
  6. Todas las pantallas se pueden usar sin scroll horizontal a 360 px de ancho y aprovechan el espacio a 1440 px; seleccionar, votar y dibujar caras funciona con el dedo en móvil.

## 11. Fuera de alcance (versión inicial)

- Login federado (OAuth, SSO).
- Asignación automática de identidades sin votación (las sugerencias de 3.5 siempre pasan por propuesta y votos).
- Sincronización inversa desde Immich.
- Comentarios o chat en las fotos.
- Notificaciones por email.
- Exportar las relaciones entre personas a Immich (XMP no tiene un estándar para ellas).

## 12. Plan de implementación sugerido

1. Esqueleto del proyecto, Dockerfile, `docker-compose.yml`, `AGENTS.md` y tests.
2. App `scanner` completa con tests.
3. App `accounts`: superadmin, gestión de usuarios, login y cambio obligatorio de contraseña.
4. Modelos de `catalog` y galería en solo lectura, con la maquetación responsive base.
5. Vista de foto con propuestas y votaciones.
6. Administración (umbrales, revisión, fusión de personas).
7. App `exporter` y prueba con Immich.
8. Copias de seguridad y README de despliegue en Portainer con Tailscale Funnel.
9. Árbol genealógico y relaciones (5.12), en subfases: modelos y servicios con tests; sección de la ficha y votación; árbol familiar; red de relaciones; integración con Inicio, avisos y gamificación.
