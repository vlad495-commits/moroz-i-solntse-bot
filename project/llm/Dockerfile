FROM python:3.12-slim

WORKDIR /app

# Системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Europe/Moscow \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

# Зависимости (отдельный слой для кэширования)
COPY llm/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код
COPY llm/ .
COPY src/ /app/src/

# Папка для логов (volume :rw в docker-compose)
RUN mkdir -p /app/logs

# Non-root system user без home
RUN groupadd --system appuser && \
    useradd --system --gid appuser --no-create-home appuser && \
    chown -R appuser:appuser /app
USER appuser

CMD ["sh", "-c", "if [ \"${TELEGRAM_MODE:-webhook}\" = polling ]; then exec python bot.py; else exec uvicorn webhook:app --host 0.0.0.0 --port 8081; fi"]
