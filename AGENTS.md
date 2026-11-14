# PhotoTag — Instrucciones para agentes

Este fichero define **cómo trabajar** en el proyecto. **Qué** construir está en `docs/specification.md`, que es la fuente de verdad: stack, arquitectura, modelo de datos, seguridad, despliegue y criterios de aceptación.

## Antes de cada tarea

- Lee solo la sección de la especificación que corresponda a la tarea; no la cargues entera si no hace falta.
- Si algo no está en la especificación o es ambiguo, pregunta antes de decidir. No inventes requisitos.
- Pregunta antes de añadir dependencias que no figuren en la especificación.
- No modifiques `docs/specification.md` salvo que se te pida expresamente.
- Trabaja por fases (sección 12 de la especificación) e implementa solo lo que se te pida.

## Comandos

```bash
docker compose up --build                                  # levanta web, worker y db
docker compose exec web python manage.py migrate
docker compose exec web python manage.py process_scans     # procesa escaneos pendientes
pytest                                                     # tests
ruff check . && ruff format --check .                      # linting y formato
```

Una tarea no está terminada hasta que los tests pasan y `ruff` no da errores.

## Convenciones de código

- Código, identificadores, comentarios y commits en **inglés**; textos de la interfaz en **español**, siempre marcados para traducción (`{% translate %}`, `{% blocktranslate %}`, `gettext`/`gettext_lazy`).
- Todo texto nuevo de la interfaz se traduce al inglés en `locale/en/LC_MESSAGES/django.po` y se recompila el `.mo` (ambos se commitean; ver README). `tests/test_ui.py` falla si queda alguna entrada sin traducir.
- La lógica de negocio (votación, recorte, exportación) va en módulos de servicio testeables, no en las vistas.
- Cada funcionalidad nueva incluye sus tests.
- Toda pantalla nueva cumple los requisitos de responsive de la sección 5.2 de la especificación.

## Reglas que nunca se rompen

Resumen de la sección 7 de la especificación, que manda en caso de duda:

- Ningún secreto en el código ni en el repositorio: siempre variables de entorno.
- Toda vista e imagen exige sesión iniciada; las imágenes nunca se sirven como estáticos públicos.
- `db` y `worker` no publican puertos.

## Commits

- Formato [Conventional Commits](https://www.conventionalcommits.org/): `<type>(<scope>): <description>`.
  - Tipos: `feat`, `fix`, `refactor`, `test`, `docs`, `style`, `chore`, `build`, `ci`, `perf`.
  - Scopes: `scanner`, `catalog`, `voting`, `accounts`, `exporter`, `ui`, `deploy`, `docs`.
  - Cambios incompatibles: `!` tras el scope y pie `BREAKING CHANGE:`.
- Mensajes simples: solo la línea de Conventional Commit, sin cuerpo ni descripción adicional.
- **Nunca añadas `Co-Authored-By`** ni ninguna otra línea de atribución a los commits.
- Al terminar un cambio con entidad de funcionalidad nueva, **propón los comandos git** agrupados en commits atómicos por propósito:

  ```bash
  git add scanner/
  git commit -m "feat(scanner): detect faces in cropped photos with YuNet"

  git add tests/scanner/
  git commit -m "test(scanner): add synthetic scans for face detection"
  ```

- Para cambios menores basta con un único commit.
- **No ejecutes `git commit` ni `git push`**: solo propón los comandos.
