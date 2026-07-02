"""Aiogram-хендлеры: /start, текстовые сообщения, нетекстовые."""

import logging

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.enums import ChatAction

import cache
import db
from config import (
    INPUT_TOO_LONG_REPLY,
    MAX_INPUT_LENGTH,
    NON_TEXT_REPLY,
    START_REPLY,
)
from llm import generate_response

logger = logging.getLogger(__name__)
router = Router()


@router.message(Command("start"))
async def handle_start(message: Message) -> None:
    """Команда /start — приветствие."""
    await message.answer(START_REPLY)


@router.message(lambda msg: msg.text and not msg.text.startswith("/"))
async def handle_text(message: Message, bot: Bot) -> None:
    """Любое текстовое сообщение (кроме команд) → LLM → ответ."""
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else None
    username = message.from_user.username if message.from_user else None
    text = message.text or ""

    # Лимит длины
    if len(text) > MAX_INPUT_LENGTH:
        await message.answer(INPUT_TOO_LONG_REPLY.format(limit=MAX_INPUT_LENGTH))
        return

    # Сохраняем входящее
    await db.save_message(chat_id, user_id, "user", text, username)

    # typing indicator
    try:
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
    except Exception:
        pass

    # Контекст из Redis (быстрее) или fallback на БД
    context = await cache.get_context(chat_id)
    if not context:
        context = await db.get_context(chat_id)

    # Уберём из контекста дубль текущего сообщения
    if context and context[-1].get("role") == "user" and context[-1].get("content") == text:
        context = context[:-1]

    # LLM
    try:
        result = await generate_response(text, context)
    except Exception:
        logger.exception("LLM упал для chat %s", chat_id)
        await message.answer(
            "Извините, временно не могу ответить. Попробуйте через минуту."
        )
        return

    # Сохраняем ответ
    await cache.push_message(chat_id, "user", text)
    await cache.push_message(chat_id, "assistant", result.text)
    await db.save_message(chat_id, user_id, "assistant", result.text)

    # Отправка пользователю
    await message.answer(result.text)


@router.message()
async def handle_non_text(message: Message) -> None:
    """Любое нетекстовое сообщение (фото, голос, видео, документ, стикер)."""
    await message.answer(NON_TEXT_REPLY)
