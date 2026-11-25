"""A recording stand-in for the Bot API client, and the composed adapter built around it, for tests that drive
the adapter's handlers directly (checks/test_action_events.py, checks/test_widget_events.py).

The stores are the real ones under a temporary Hermes home; what a test asserts is what the agent received
(``adapter.received``, captured from ``handle_message``) and what Telegram was asked to do (the bot's calls).
"""

import asyncio
from types import SimpleNamespace
from typing import Any

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageEvent
from hermes_telegram.adapter import HermesTelegramAdapter

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
