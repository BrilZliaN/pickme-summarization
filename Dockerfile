FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

# Create non-root user
RUN useradd --create-home --shell /bin/bash app

WORKDIR /app

# Install dependencies first (better layer caching)
COPY pyproject.toml README.md ./
COPY src ./src
COPY migrations ./migrations

RUN pip install --no-cache-dir .

# Data directory owned by non-root user
RUN mkdir -p /app/data && chown -R app:app /app

USER app

CMD ["python", "-m", "pickme.main"]
