"""A recording stand-in for the Bot API client, and the composed adapter built around it, for tests that drive
the adapter's handlers directly (checks/test_action_events.py, checks/test_widget_events.py).

The stores are the real ones under a temporary Hermes home; what a test asserts is what the agent received
(``adapter.received``, captured from ``handle_message``) and what Telegram was asked to do (the bot's calls).
"""

import asyncio
from types import SimpleNamespace
from typing import Any

from telegram.error import TimedOut

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageEvent
from hermes_telegram.adapter import HermesTelegramAdapter
from hermes_telegram.buttons import PREFIX

OWNER, KID, STRANGER = "111", "222", "333"


class Bot:
    """The Bot API client: records every call; ``fail[method]`` raises its errors in order, ``hold[method]``
    keeps the call open until the test sets the event."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail: dict[str, list[Exception]] = {}
        self.hold: dict[str, asyncio.Event] = {}
        self._ids = iter(range(900, 10_000))
        self.sticker_sets: dict[str, list[str]] = {}

    async def _call(self, method: str, kwargs: dict[str, Any]) -> int:
        self.calls.append((method, kwargs))
        if method in self.hold:
            await self.hold[method].wait()
        if self.fail.get(method):
            raise self.fail[method].pop(0)
        return next(self._ids)

    def of(self, method: str) -> list[dict[str, Any]]:
        return [kwargs for name, kwargs in self.calls if name == method]

    async def send_poll(self, **kwargs: Any) -> Any:
        mid = await self._call("sendPoll", kwargs)
        return SimpleNamespace(message_id=mid, poll=SimpleNamespace(id=f"poll-{mid}"))

    async def send_sticker(self, **kwargs: Any) -> Any:
        return SimpleNamespace(message_id=await self._call("sendSticker", kwargs))

    async def send_message(self, **kwargs: Any) -> Any:
        kwargs.update(kwargs.pop("api_kwargs", None) or {})  # what Telegram receives
        return SimpleNamespace(message_id=await self._call("sendMessage", kwargs))

    async def do_api_request(self, method: str, api_kwargs: dict[str, Any]) -> Any:
        return {"message_id": await self._call(method, api_kwargs)}

    async def set_message_reaction(self, **kwargs: Any) -> bool:
        await self._call("setMessageReaction", kwargs)
        return True

    async def edit_message_text(self, **kwargs: Any) -> Any:
        kwargs.update(kwargs.pop("api_kwargs", None) or {})
        await self._call("editMessageText", kwargs)
        return SimpleNamespace(message_id=kwargs.get("message_id"))

    async def delete_message(self, **kwargs: Any) -> bool:
        await self._call("deleteMessage", kwargs)
        return True

    async def pin_chat_message(self, **kwargs: Any) -> bool:
        await self._call("pinChatMessage", kwargs)
        return True

    async def unpin_chat_message(self, **kwargs: Any) -> bool:
        await self._call("unpinChatMessage", kwargs)
        return True

    async def get_sticker_set(self, name: str) -> Any:
        await self._call("getStickerSet", {"name": name})
        return SimpleNamespace(name=name, title=name.title(), stickers=[
            SimpleNamespace(file_id=fid, emoji="👍") for fid in self.sticker_sets[name]])


def build_adapter(home, **extra: Any) -> HermesTelegramAdapter:
    adapter = HermesTelegramAdapter(PlatformConfig(enabled=True, token="1:x", extra={
        "allow_from": [OWNER, KID], **extra}))
    adapter._update_receipt_dir = home
    adapter._bot = adapter.bot_stub = Bot()
    adapter.received = []

    async def handle_message(event: MessageEvent) -> None:
        adapter.received.append(event)
    adapter.handle_message = handle_message
    return adapter


def user(user_id: str) -> Any:
    return SimpleNamespace(id=int(user_id), is_bot=False, full_name=f"User {user_id}", first_name="User")


def message(chat_id: str, message_id: Any, thread_id: Any = None, chat_type: str = "private") -> Any:
    return SimpleNamespace(message_id=int(message_id), chat_id=int(chat_id),
                           chat=SimpleNamespace(id=int(chat_id), type=chat_type),
                           message_thread_id=int(thread_id) if thread_id else None,
                           is_topic_message=bool(thread_id))


class Query(SimpleNamespace):
    """A tap; ``answer`` is Telegram's acknowledgement, which can fail."""

    async def answer(self, text: str | None = None) -> None:
        self.answers.append(text)
        if self.answer_fails:
            raise TimedOut("query is too old")


_queries = iter(range(1, 10_000))


async def tap(adapter, user_id: str, message: Any, token: str, *, answer_fails: bool = False) -> Query:
    query = Query(id=str(next(_queries)), data=PREFIX + token, from_user=user(user_id), message=message,
                  answers=[], answer_fails=answer_fails)
    await adapter._handle_callback_query(SimpleNamespace(callback_query=query), None)
    return query
