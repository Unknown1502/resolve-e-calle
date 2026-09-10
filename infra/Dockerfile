# Builds both the api and worker services. One image, two commands.

FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --silent
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY pyproject.toml README.md alembic.ini ./
COPY backend/ ./backend/
RUN pip install --no-cache-dir -e .

# The dashboard is served by the API process, from the same origin, so
# the browser never needs a second host and never holds a CALL-E key.
COPY --from=frontend /build/dist ./frontend/dist

ENV PYTHONPATH=/srv/backend

# Run as a non-root user.
RUN useradd --create-home --uid 10001 resolvee && chown -R resolvee /srv
USER resolvee

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
