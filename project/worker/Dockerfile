FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

COPY llm/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY worker /app/worker
COPY src /app/src

RUN groupadd --system appuser && \
    useradd --system --gid appuser --no-create-home appuser && \
    chown -R appuser:appuser /app
USER appuser

CMD ["python", "worker/main.py"]
