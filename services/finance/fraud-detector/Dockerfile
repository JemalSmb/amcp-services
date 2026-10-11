FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY shared/ /app/shared/
COPY services/finance/fraud-detector/service.py /app/services/finance/fraud-detector/service.py

ENV PYTHONPATH=/app
EXPOSE 8000

CMD ["python", "services/finance/fraud-detector/service.py"]
