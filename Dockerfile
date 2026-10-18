FROM python:3.13.1-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY requirements.lock /app/requirements.lock
RUN python -m pip install --no-cache-dir --requirement /app/requirements.lock

COPY pyproject.toml /app/pyproject.toml
COPY src /app/src
COPY frontend /app/frontend
COPY tests /app/tests

# Safe default: run the offline test suite rather than bind a network port.
CMD ["python", "-m", "pytest", "-q"]
