from __future__ import annotations

import re
from dataclasses import dataclass

from moroz.booking.interaction import WorkflowReply
from moroz.messaging.models import ScenarioResult


_ACTION_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


@dataclass(frozen=True, slots=True)
class PresentedAction:
    text: str
    action_id: str

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("button text is required")
        if not _ACTION_ID.fullmatch(self.action_id) or self.action_id.startswith(
            "booking"
        ):
            raise ValueError("action id must be short and opaque")


class BookingPresenter:
    def plain(self, text: str) -> WorkflowReply:
        return WorkflowReply(text, {})

    def choice(
        self,
        text: str,
        actions: tuple[PresentedAction, ...] | list[PresentedAction],
    ) -> WorkflowReply:
        rows = [
            [
                {
                    "text": action.text,
                    "callback_data": f"booking:{action.action_id}",
                }
            ]
            for action in actions
        ]
        return WorkflowReply(
            text,
            {"reply_markup": {"inline_keyboard": rows}},
        )

    def request_contact(self, text: str) -> WorkflowReply:
        return WorkflowReply(
            text,
            {
                "reply_markup": {
                    "keyboard": [
                        [
                            {
                                "text": "Отправить свой контакт",
                                "request_contact": True,
                            }
                        ],
                        [{"text": "Назад"}, {"text": "Отмена"}],
                    ],
                    "resize_keyboard": True,
                    "one_time_keyboard": True,
                }
            },
        )

    def scenario_result(self, result: ScenarioResult) -> WorkflowReply:
        if result.status == "ok":
            return self.plain("Запись подтверждена.")
        if result.status == "needs_input" and result.next_action == "choose_slot":
            return self.plain(
                "Выбранное время уже недоступно. Выберите время заново."
            )
        if result.status == "escalated":
            return self.plain(
                "Статус записи проверит администратор. "
                "Мы не обещаем слот, пока не получим однозначное подтверждение."
            )
        return self.plain("Не удалось выполнить запись. Попробуйте позже.")
