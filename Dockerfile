FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim-bookworm AS runtime
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data FRONTEND_DIR=/app/frontend/dist
WORKDIR /app
RUN groupadd --gid 10001 console && useradd --uid 10001 --gid console --no-create-home console \
    && mkdir -p /data && chown console:console /data
COPY pyproject.toml requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock \
    && python -c "from pymammotion.client import MammotionClient; from pymammotion.transport.base import AuthError"
RUN python -m playwright install --with-deps --only-shell chromium
COPY backend/ ./backend/
COPY --from=frontend /build/dist/ ./frontend/dist/
USER 10001:10001
EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/api/health', timeout=3)" || exit 1
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "5000", "--workers", "1", "--no-access-log"]
