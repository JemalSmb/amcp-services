FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY services/gateway/gateway.py /app/services/gateway/gateway.py
COPY services/gateway/auth.py /app/services/gateway/auth.py
COPY services/gateway/admin_api.py /app/services/gateway/admin_api.py
COPY services/gateway/admin_platform.py /app/services/gateway/admin_platform.py
COPY services/gateway/audit_log.py /app/services/gateway/audit_log.py
COPY services/gateway/alert_store.py /app/services/gateway/alert_store.py
COPY services/gateway/platform_api.py /app/services/gateway/platform_api.py
COPY services/gateway/record_store.py /app/services/gateway/record_store.py
COPY services/gateway/seed_data.py /app/services/gateway/seed_data.py

ENV PYTHONPATH=/app
WORKDIR /app/services/gateway
EXPOSE 8080

CMD ["python", "gateway.py"]
