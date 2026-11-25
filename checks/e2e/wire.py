"""A real Bot API client in this process: python-telegram-bot's ``Bot`` over httpx against the stand-in.

``sender`` is the composed adapter with such a bot. A Bot API refusal comes from the stand-in
(``Sender.refuse``); a connection failure comes from httpx's own transport (``Wire.fail``), so each failure
is the one PTB raises in production. Hermes' stand-in needs the pinned source: these tests are e2e.
"""

import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

import checks.e2e.rig  # noqa: F401  (puts the pinned Hermes source, and with it the stand-in, on sys.path)
from checks.e2e.standin import MediaTelegramStandin
from gateway.config import PlatformConfig
from hermes_telegram.adapter import HermesTelegramAdapter

CHAT = "111"  # a private chat


class Wire(httpx.AsyncBaseTransport):
    """httpx's own transport, with a connection failure on chosen calls of a Bot API method.

    ``refused``: the connection never opens (httpx.ConnectError). ``pool``: no pooled connection is free
    (httpx.PoolTimeout). ``lost``: the request reaches Telegram, then the connection drops before the reply
    (httpx.ReadError), so the message is on screen and the sender cannot know it. ``before`` runs as each
    request leaves, with its method."""

    def __init__(self) -> None:
        self._inner = httpx.AsyncHTTPTransport()
        self._calls: dict[str, int] = {}
        self._faults: dict[tuple[str, int], str] = {}
        self.before: Callable[[str], None] | None = None

    def fail(self, method: str, call: int, fault: str) -> None:
        """Fail the ``call``-th request (from 1) of ``method`` with ``fault``."""
        self._faults[(method, call)] = fault

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        self._calls[method] = self._calls.get(method, 0) + 1
        if self.before is not None:
            self.before(method)
        fault = self._faults.get((method, self._calls[method]))
        if fault == "refused":
            raise httpx.ConnectError("[Errno 61] Connection refused", request=request)
        if fault == "pool":
            raise httpx.PoolTimeout("no free connection", request=request)
        response = await self._inner.handle_async_request(request)
        if fault == "lost":
            await response.aread()
            raise httpx.ReadError("connection closed", request=request)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


@dataclass
class Sender:
    standin: MediaTelegramStandin
    wire: Wire
    adapter: HermesTelegramAdapter

    def shown(self, chat: str) -> list[Any]:
        return self.standin.visible(chat)

    def words(self, chat: str) -> list[str]:
        """The numbered words (``w00042``) the chat shows, in order: tests' answers are made of them."""
        return [w for v in self.shown(chat) for w in re.findall(r"w\d{5}", v.text)]

    def refuse(self, method: str, description: str, *, status: int = 400, times: int = 10_000,
               match: Callable[[dict[str, Any]], bool] | None = None, **parameters: Any) -> None:
        body: dict[str, Any] = {"ok": False, "error_code": status, "description": description}
        if parameters:
            body["parameters"] = parameters
        self.standin.fail(method, body, status=status, times=times, match=match)


@pytest.fixture(name="sender")
async def sender_fixture() -> AsyncIterator[Sender]:
    from telegram import Bot
    from telegram.request import HTTPXRequest

    standin = MediaTelegramStandin().start()
    wire = Wire()
    bot = Bot(standin.token, base_url=standin.api_base, request=HTTPXRequest(httpx_kwargs={"transport": wire}))
    await bot.initialize()
    adapter = HermesTelegramAdapter(PlatformConfig(enabled=True, token=standin.token))
    adapter._bot = bot
    adapter._telegram_chat_outbound_slot_secs = 0  # no 1 s pacing between parts in tests
    try:
        yield Sender(standin, wire, adapter)
    finally:
        await bot.shutdown()
        standin.stop()
