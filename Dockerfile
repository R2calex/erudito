FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git && \
    rm -rf /var/lib/apt/lists/* && \
    git config --global --add safe.directory '*'

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8095

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8095/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8095"]
