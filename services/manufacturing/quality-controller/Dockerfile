FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY services/manufacturing/quality-controller/service.py /app/services/manufacturing/quality-controller/service.py

ENV PYTHONPATH=/app
EXPOSE 8000

CMD ["python", "services/manufacturing/quality-controller/service.py"]
