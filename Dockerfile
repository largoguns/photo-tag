# syntax=docker/dockerfile:1

# ---- builder: resolve and install Python dependencies into a venv -------------
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /build
COPY pyproject.toml ./
# Install only the dependencies (the code is copied in the runtime stage).
RUN python - <<'PY' > requirements.txt
import tomllib
print("\n".join(tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]))
PY
RUN pip install -r requirements.txt

# ---- runtime -------------------------------------------------------------------
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    DATA_DIR=/data \
    TZ=Europe/Madrid

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libimage-exiftool-perl postgresql-client tzdata libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 1000 phototag \
    && useradd --system --uid 1000 --gid phototag --home /app --shell /usr/sbin/nologin phototag

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=phototag:phototag . .

RUN mkdir -p /data/scans/inbox /data/scans/processed /data/photos/thumbs /data/export /data/backups \
    && chown -R phototag:phototag /data \
    && chmod +x docker/entrypoint.sh \
    && DJANGO_SECRET_KEY=build-only python manage.py collectstatic --noinput \
    && chown -R phototag:phototag /app/staticfiles

USER phototag
EXPOSE 8000
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["web"]

# ---- test: runtime + dev tools (docker build --target test .) ------------------
FROM runtime AS test
USER root
RUN pip install --no-cache-dir "pytest>=8.3" "pytest-django>=4.9" "ruff>=0.7"
USER phototag
ENTRYPOINT []
CMD ["pytest", "-q", "-p", "no:cacheprovider"]
