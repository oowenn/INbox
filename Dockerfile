FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Data + secrets live on the bind mount at /app/host (see docker-compose.yml).
ENV JOBINBOX_PROJECT_ROOT=/app/host

COPY pyproject.toml README.md ./
COPY src ./src
COPY index.html ./index.html
COPY DESIGN.md ./DESIGN.md
COPY env.example ./env.example

RUN pip install --no-cache-dir .
RUN mkdir -p /app/host

EXPOSE 8000

CMD ["python", "-m", "jobinbox", "serve", "--host", "0.0.0.0", "--port", "8000"]
