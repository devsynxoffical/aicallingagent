FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY callagent ./callagent
RUN pip install --upgrade pip && pip install .

VOLUME ["/app/data"]
EXPOSE 8000
CMD ["callagent", "serve", "--host", "0.0.0.0", "--port", "8000"]
