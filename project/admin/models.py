"""Pydantic-модели для админки (для type hints и валидации форм)."""

from datetime import datetime

from pydantic import BaseModel


class ChatItem(BaseModel):
    chat_id: int
    user_id: int | None = None
    username: str | None = None
    message_count: int
    last_message_at: datetime
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    llm_calls: int = 0
    last_model: str | None = None


class ChatStats(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    llm_calls: int = 0
    last_model: str | None = None


class GlobalStats(BaseModel):
    total_chats: int = 0
    total_users: int = 0
    total_messages: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    total_llm_calls: int = 0
    total_incidents: int = 0
