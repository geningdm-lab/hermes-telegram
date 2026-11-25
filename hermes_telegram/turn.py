"""The Telegram turn an agent tool runs in: its chat, its guards, and the adapter and loop that serve it.

docs/design.md (Agent interface, Tools) sets three guards, which every tool here passes through ``Turn``:

- The model never names the recipient: the chat and topic are the turn's own, from Hermes' session context
  (``gateway/session_context.py``), never a tool argument.
- Tools act only in a turn a user started in a Telegram chat: not in cron runs, not on other surfaces.
- A turn sends at most ``MAX_SENDS_PER_TURN`` extra messages.

Reaching the live adapter follows Hermes' own send tool (``tools/send_message_senders.py:_live_adapter``):
the gateway runner, then the adapter of the active profile, which under ``multiplex_profiles`` is the
profile the turn was routed to. Tool handlers run on a worker loop (``model_tools._run_async``), so adapter
calls are scheduled on the gateway's loop, as ``tools/send_message_tool.py:_dispatch_on_gateway_loop`` does.
"""

import asyncio
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

MAX_SENDS_PER_TURN = 10
_TURN_MEMORY = 256  # turns whose send counts are remembered

T = TypeVar("T")


class TurnRefused(Exception):
    """A tool call this turn may not make; the message is for the agent."""


@dataclass(frozen=True)
class Turn:
    session_key: str
    chat_id: str
    thread_id: str | None
    inbound_message_id: str
    profile: str

    @property
    def metadata(self) -> dict[str, Any]:
        """Routing metadata for the adapter's send methods: the turn's topic, if any."""
        return {"thread_id": self.thread_id} if self.thread_id else {}

    @classmethod
    def current(cls) -> "Turn":
        """The Telegram turn running in this context, or ``TurnRefused`` saying why there is none."""
        from gateway.session_context import get_session_env

        platform = get_session_env("HERMES_SESSION_PLATFORM", "")
        if platform != "telegram":
            raise TurnRefused("Telegram tools work only in a Telegram chat turn.")
        if get_session_env("HERMES_CRON_SESSION", ""):
            raise TurnRefused("Telegram tools do not run in scheduled (cron) turns.")
        chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
        session_key = get_session_env("HERMES_SESSION_KEY", "")
        if not chat_id or not session_key:
            raise TurnRefused("This turn has no Telegram chat to act in.")
        return cls(
            session_key=session_key, chat_id=chat_id,
            thread_id=get_session_env("HERMES_SESSION_THREAD_ID", "") or None,
            inbound_message_id=get_session_env("HERMES_SESSION_MESSAGE_ID", ""),
            profile=get_session_env("HERMES_SESSION_PROFILE", ""))


class SendBudget:
    """How many extra messages each recent turn has sent, bounded in memory."""

    def __init__(self, limit: int = MAX_SENDS_PER_TURN) -> None:
        self.limit = limit
        self._counts: OrderedDict[tuple[str, str], int] = OrderedDict()
        self._lock = threading.Lock()

    def take(self, turn: Turn, count: int = 1) -> None:
        """Reserve ``count`` sends for ``turn`` or refuse without reserving any."""
        key = (turn.session_key, turn.inbound_message_id)
        with self._lock:
            used = self._counts.get(key, 0)
            if used + count > self.limit:
                raise TurnRefused(
                    f"This turn may send at most {self.limit} extra messages; {used} already sent. "
                    "Put the rest in your answer.")
            self._counts[key] = used + count
            self._counts.move_to_end(key)
            while len(self._counts) > _TURN_MEMORY:
                self._counts.popitem(last=False)


def live_adapter() -> tuple[Any, Any]:
    """``(runner, adapter)`` serving Telegram for the active profile, or ``TurnRefused``."""
    from gateway.config import Platform
    from gateway.run import _gateway_runner_ref
    from hermes_cli.profiles import get_active_profile_name

    runner = _gateway_runner_ref()
    if runner is None:
        raise TurnRefused("No Telegram gateway runs in this process.")
    resolve = getattr(runner, "_authorization_adapter", None)
    adapter = (resolve(Platform.TELEGRAM, get_active_profile_name()) if callable(resolve)
               else runner.adapters.get(Platform.TELEGRAM))
    if adapter is None:
        raise TurnRefused("This profile has no Telegram adapter.")
    return runner, adapter


async def on_gateway_loop(runner: Any, make: Callable[[], Awaitable[T]]) -> T:
    """Await ``make()`` on the gateway's loop: adapter state (locks, the bot's HTTP client) belongs to it."""
    loop = getattr(runner, "_gateway_loop", None)
    if loop is None or loop is asyncio.get_running_loop():
        return await make()
    if not loop.is_running():
        raise TurnRefused("The Telegram gateway is not running.")
    future = asyncio.run_coroutine_threadsafe(make(), loop)
    # A cancelled caller must not cancel a send already queued there: a retry would duplicate it.
    return await asyncio.shield(asyncio.wrap_future(future))
