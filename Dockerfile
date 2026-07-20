FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# .dockerignore keeps config/, caches, and build helpers out of the image
COPY *.py ./

ENV DASHBOARD_PORT=8080 \
    POLL_SECONDS=10 \
    SOCKET_TIMEOUT_SECONDS=4 \
    POLL_WORKERS=16 \
    CONFIG_DIR=/app/config

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('DASHBOARD_PORT','8080')+'/api/status',timeout=4)"

# Shell form so DASHBOARD_PORT is honored — exec-form arrays don't expand env vars
CMD uvicorn app:app --host 0.0.0.0 --port ${DASHBOARD_PORT:-8080}
