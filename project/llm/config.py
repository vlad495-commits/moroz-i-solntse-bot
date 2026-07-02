"""Конфигурация LLM-контейнера. Все настройки читаются из переменных окружения."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Корневой .env (на 2 уровня выше: project/llm/ → project/ → корень)
_ROOT_ENV = Path(__file__).resolve().parent.parent.parent / ".env"
if _ROOT_ENV.exists():
    load_dotenv(_ROOT_ENV)

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# --- LLM (универсальное подключение к любому провайдеру) ---
# LLM_API_KEY — ключ для основного провайдера (OpenAI / Anthropic / OpenRouter / DeepSeek / любой OpenAI-совместимый)
# LLM_BASE_URL — endpoint провайдера. Пусто → дефолт OpenAI (https://api.openai.com/v1).
#   Для Anthropic — оставь пусто и поставь LLM_MODEL=claude-* (использует AsyncAnthropic).
#   Для OpenRouter — LLM_BASE_URL=https://openrouter.ai/api/v1
#   Для DeepSeek — LLM_BASE_URL=https://api.deepseek.com
#   Для локального Ollama — LLM_BASE_URL=http://host.docker.internal:11434/v1
LLM_API_KEY = os.getenv("LLM_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "") or None
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4.1-mini")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.3"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "2000"))
# Таймаут одного запроса к LLM (сек): зависший провайдер рвётся за это время,
# а не за дефолтные ~600с SDK.
LLM_REQUEST_TIMEOUT_SEC = int(os.getenv("LLM_REQUEST_TIMEOUT_SEC", "30"))

# --- Хранилища ---
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
DATABASE_URL = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    _pg_user = os.getenv("POSTGRES_USER", "")
    _pg_pass = os.getenv("POSTGRES_PASSWORD", "")
    _pg_db = os.getenv("POSTGRES_DB", "")
    if _pg_user and _pg_pass and _pg_db:
        DATABASE_URL = f"postgresql://{_pg_user}:{_pg_pass}@postgres:5432/{_pg_db}"
CONTEXT_MESSAGES_LIMIT = int(os.getenv("CONTEXT_MESSAGES_LIMIT", "20"))

# --- Промпт ---
SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "system.md"

# --- Сообщения ---
MAX_INPUT_LENGTH = int(os.getenv("MAX_INPUT_LENGTH", "4000"))
INPUT_TOO_LONG_REPLY = os.getenv(
    "INPUT_TOO_LONG_REPLY",
    "Сообщение слишком длинное. Сократите его, пожалуйста (лимит {limit} символов).",
)
START_REPLY = os.getenv(
    "START_REPLY",
    "Привет! Я ИИ-ассистент. Задайте свой вопрос текстом, и я постараюсь помочь.",
)
NON_TEXT_REPLY = os.getenv(
    "NON_TEXT_REPLY",
    "Я понимаю только текстовые сообщения. Напишите ваш вопрос текстом, пожалуйста.",
)

# --- Логирование ---
LOG_FILE = os.getenv("LOG_FILE", "/app/logs/bot.log")
LOG_FILE_MAX_BYTES = int(os.getenv("LOG_FILE_MAX_BYTES", str(10 * 1024 * 1024)))
LOG_FILE_BACKUPS = int(os.getenv("LOG_FILE_BACKUPS", "5"))

# --- Graceful shutdown ---
SHUTDOWN_INFLIGHT_TIMEOUT_SEC = int(os.getenv("SHUTDOWN_INFLIGHT_TIMEOUT_SEC", "15"))

# --- Хранение данных ---
# Сколько дней хранить записи в Postgres (messages).
# Каждые сутки фоновая задача удаляет старше этого срока. По дефолту 3 года.
# 0 или отрицательное значение = выключить автоочистку (хранить вечно).
DATA_RETENTION_DAYS = int(os.getenv("DATA_RETENTION_DAYS", "1095"))
