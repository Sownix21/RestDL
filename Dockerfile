FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN useradd --create-home --uid 10001 dloader
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /app/database /app/downloads /app/logs && chown -R dloader:dloader /app
USER dloader
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 CMD ["python", "scripts/healthcheck.py"]
CMD ["python", "main.py"]
