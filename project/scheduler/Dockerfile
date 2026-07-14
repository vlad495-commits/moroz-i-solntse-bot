FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY scheduler /app/scheduler

RUN groupadd --system appuser && \
    useradd --system --gid appuser --no-create-home appuser && \
    chown -R appuser:appuser /app
USER appuser

CMD ["python", "scheduler/main.py"]
