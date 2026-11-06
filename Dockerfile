# Production image for Railway. Migrations run right before the app starts.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY alembic.ini ./
COPY app ./app

RUN useradd --create-home --uid 10001 appuser
USER appuser

# Railway injects PORT; the app reads it. /health is used by the Railway health check.
CMD ["sh", "-c", "alembic upgrade head && python -m app.main"]
