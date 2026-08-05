FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src:/app/llm

COPY worker/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY worker /app/worker
COPY src /app/src
COPY llm/llm.py llm/config.py /app/llm/
COPY llm/prompts /app/llm/prompts

RUN groupadd --system --gid 10001 appuser && \
    useradd --system --uid 10001 --gid 10001 --no-create-home appuser && \
    chown -R appuser:appuser /app
USER appuser

CMD ["python", "worker/main.py"]
