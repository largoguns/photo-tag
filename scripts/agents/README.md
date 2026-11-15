# Scripts para agentes

Utilidades de desarrollo que usan los agentes (y cualquiera) para tareas repetitivas. No se usan en producción ni están en la imagen Docker.

| Script | Para qué |
|---|---|
| `po_untranslated.py` | Lista en JSON las entradas del `.po` vacías o *fuzzy*. |
| `po_fill.py` | Aplica un JSON de traducciones a esas entradas, sin tocar las existentes. |
| `seed_demo.py` | Rellena una base de datos local desechable con usuarios, escaneos, fechas y propuestas de ejemplo. |
| `ui_check.mjs` | Recorre las pantallas con Playwright a 320–1440 px, en claro/oscuro y es/en: hace capturas y detecta scroll horizontal y textos cortados. |

## Flujo de traducciones

```bash
docker run --rm -v "$PWD":/app -w /app -e DJANGO_DEBUG=1 phototag:test \
  python manage.py makemessages -l en --no-obsolete --ignore=.venv --ignore=tests --ignore=scripts
python scripts/agents/po_untranslated.py locale/en/LC_MESSAGES/django.po > /tmp/todo.json
# … rellenar /tmp/todo.json …
python scripts/agents/po_fill.py locale/en/LC_MESSAGES/django.po /tmp/todo.json
docker run --rm -v "$PWD":/app -w /app/locale/en/LC_MESSAGES phototag:test \
  msgfmt --check -o django.mo django.po
```

## Revisión visual

```bash
export DJANGO_DEBUG=1 DATA_DIR=/tmp/phototag-demo DATABASE_URL=sqlite:////tmp/phototag-demo/db.sqlite3
python manage.py migrate && python scripts/agents/seed_demo.py
python manage.py runserver 127.0.0.1:8765 &
NODE_PATH=/tmp/pw/node_modules node scripts/agents/ui_check.mjs http://127.0.0.1:8765 /tmp/ui-shots
```

Playwright no es dependencia del proyecto: instálalo en una carpeta temporal (ver la cabecera de `ui_check.mjs`).

Si la descarga de Chromium de `npx playwright install` falla o va lenta, usa la imagen oficial, que ya trae los navegadores (solo hace falta el paquete npm `playwright` de la misma versión):

```bash
mkdir -p /tmp/pw && (cd /tmp/pw && npm init -y >/dev/null && npm i playwright@1.49.1)
docker run --rm --network host -v "$PWD":/work -v /tmp/pw:/pw -w /work \
  -e NODE_PATH=/pw/node_modules mcr.microsoft.com/playwright:v1.49.1-noble \
  node scripts/agents/ui_check.mjs http://127.0.0.1:8765 /work/.ui-shots
```
