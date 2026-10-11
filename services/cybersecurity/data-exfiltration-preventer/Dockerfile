FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY services/cybersecurity/data-exfiltration-preventer/service.py /app/services/cybersecurity/data-exfiltration-preventer/service.py

ENV PYTHONPATH=/app
EXPOSE 8000

CMD ["python", "services/cybersecurity/data-exfiltration-preventer/service.py"]
