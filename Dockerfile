FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py shared.py devices.py sis.py config_store.py dms_control.py dms_names.py mtx_engine.py matrix12800_control.py matrix12800_names.py smx_control.py smx_names.py modules_store.py routes_dms.py routes_mtx_config.py routes_matrix12800.py routes_smx.py routes_ipcp505.py ./

ENV DASHBOARD_PORT=8080 \
    POLL_SECONDS=10 \
    SOCKET_TIMEOUT_SECONDS=4 \
    POLL_WORKERS=16 \
    CONFIG_DIR=/app/config

EXPOSE 8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
