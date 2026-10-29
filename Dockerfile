FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY alembic.ini ./
COPY migrations/ ./migrations/

RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 app

USER app

EXPOSE 8000

CMD ["uvicorn", "cre.api:app", "--host", "0.0.0.0", "--port", "8000"]
