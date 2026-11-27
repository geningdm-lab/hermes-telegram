"""The Telegram turn an agent tool runs in: its chat, its guards, and the adapter and loop that serve it.

docs/design.md (Agent interface, Tools) sets three guards, which every tool here passes through:

- The model never names the recipient: the chat and topic are the turn's own, never a tool argument.
- Tools act only in a turn a user's own Telegram message started. ``HumanTurnsMixin`` records each such
  event as the adapter starts processing it (``on_processing_start``) and forgets it when processing ends;
  a tool finds its turn there or is refused. Cron runs, loop and goal wakeups (internal events, whose
  synthetic source carries no message id: ``gateway/run_goals.py:112``) and other surfaces have no record.
- A turn sends at most ``MAX_SENDS_PER_TURN`` extra messages, counted on that record, so the count lives
  exactly as long as the turn.

``Turn`` is what a tool reads from Hermes' session context (``gateway/session_context.py``) on its own
thread; ``AdmittedTurn`` is the adapter's record, read on the gateway loop. Sends use the record's routing
metadata, built by Hermes' own ``_thread_metadata_for_event``. A tool's send keeps the turn's topic: while
``TOOL_SEND`` is set, the fallbacks that ordinary delivery has when the topic is gone (a text, a media send,
an album or one of Hermes' prompts sent to the root chat instead) do not apply, and the send fails instead.
A deleted reply anchor is dropped as in ordinary delivery: that retry stays in the topic.

Reaching the live adapter follows Hermes' own send tool (``tools/send_message_senders.py:_live_adapter``):
the gateway runner, then the adapter of the active profile, which under ``multiplex_profiles`` is the
profile the turn was routed to. Tool handlers run on a worker loop (``model_tools._run_async``), so adapter
calls are scheduled on the gateway's loop, as ``tools/send_message_tool.py:_dispatch_on_gateway_loop`` does.
"""

import asyncio
import contextvars
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Optional, TypeVar

MAX_SENDS_PER_TURN = 10
# Turns being processed at once, far above any real number; past it the oldest record is dropped.
_TURN_MEMORY = 4096

T = TypeVar("T")

# The refusal a tool's text gets when its topic is gone. It names no topic marker that the harvested chunk
# loop reroutes on (``telegram_dm_topic_send.topic_gone``) or drops the reply anchor for.
TOPIC_GONE = "the topic of this turn no longer takes messages, and nothing was sent outside it"

# Set while a tool's own sends run on the gateway loop (``as_tool_send``).
TOOL_SEND: contextvars.ContextVar[bool] = contextvars.ContextVar("hermes_telegram_tool_send", default=False)


class TurnRefused(Exception):
    """A tool call this turn may not make; the message is for the agent."""


@dataclass(frozen=True)
class Turn:
    """The turn a tool call runs in, as Hermes' session context names it."""

    session_key: str
    chat_id: str
    thread_id: str | None
    inbound_message_id: str
    profile: str

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
        message_id = get_session_env("HERMES_SESSION_MESSAGE_ID", "")
        if not message_id:
            raise TurnRefused(_NOT_A_USER_TURN)
        return cls(
            session_key=session_key, chat_id=chat_id,
            thread_id=get_session_env("HERMES_SESSION_THREAD_ID", "") or None,
            inbound_message_id=message_id,
            profile=get_session_env("HERMES_SESSION_PROFILE", ""))


_NOT_A_USER_TURN = ("Telegram tools work only in a turn started by the user's own Telegram message, not in "
                    "scheduled, automatic or background turns.")


@dataclass
class AdmittedTurn:
    """A human Telegram event the adapter is processing: where its sends go, and how many it has sent."""

    chat_id: str
    thread_id: str | None
    message_id: str
    metadata: dict[str, Any]
    limit: int = MAX_SENDS_PER_TURN
    sent: int = field(default=0)
    # The user's message as Telegram delivered it (text or caption, and its entities as Bot API dicts), for a
    # quote of it; None for a tap, vote, reaction or widget request, which has no text of its own.
    text: str | None = None
    entities: list[dict[str, Any]] = field(default_factory=list)

    def take(self, count: int) -> None:
        """Reserve ``count`` sends or refuse without reserving any. Runs on the gateway loop only."""
        if self.sent + count > self.limit:
            raise TurnRefused(
                f"This turn may send at most {self.limit} extra messages; {self.sent} already sent and this "
                f"call needs {count}. Put the rest in your answer.")
        self.sent += count

    def try_take(self, count: int = 1) -> bool:
        if self.sent + count > self.limit:
            return False
        self.sent += count
        return True

    def give_back(self, count: int) -> None:
        self.sent = max(0, self.sent - count)


def _routed_to_topic(send_kwargs: dict[str, Any]) -> bool:
    return bool(send_kwargs.get("message_thread_id") or send_kwargs.get("direct_messages_topic_id"))


def _event_key(chat_id: Any, message_id: Any) -> tuple[str, str]:
    return str(chat_id), str(message_id)


class HumanTurnsMixin:
    """Records the human Telegram events being processed; listed before the harvested mixins."""

    def _human_turns(self) -> "OrderedDict[tuple[str, str], AdmittedTurn]":
        return self.__dict__.setdefault("_hermes_telegram_human_turns", OrderedDict())

    async def on_processing_start(self, event: Any) -> None:
        await super().on_processing_start(event)
        source = getattr(event, "source", None)
        message_id = getattr(event, "message_id", None) or getattr(source, "message_id", None)
        if getattr(event, "internal", False) or source is None or not message_id or not getattr(source, "chat_id", None):
            return
        from gateway.platforms.base import _thread_metadata_for_event

        turns = self._human_turns()
        raw = getattr(event, "raw_message", None)
        text = (getattr(raw, "text", None) or getattr(raw, "caption", None)) if raw is not None else None
        entities = (getattr(raw, "entities", None) or getattr(raw, "caption_entities", None) or ()) if text else ()
        turns[_event_key(source.chat_id, message_id)] = AdmittedTurn(
            chat_id=str(source.chat_id), thread_id=str(source.thread_id) if source.thread_id else None,
            message_id=str(message_id), metadata=dict(_thread_metadata_for_event(event) or {}),
            text=text if isinstance(text, str) else None,
            entities=[e.to_dict() if hasattr(e, "to_dict") else dict(e) for e in entities])
        while len(turns) > _TURN_MEMORY:
            turns.popitem(last=False)

    async def on_processing_complete(self, event: Any, outcome: Any) -> None:
        source = getattr(event, "source", None)
        message_id = getattr(event, "message_id", None) or getattr(source, "message_id", None)
        if source is not None and message_id:
            self._human_turns().pop(_event_key(source.chat_id, message_id), None)
        await super().on_processing_complete(event, outcome)

    @staticmethod
    def _is_thread_not_found_error(error: Exception) -> bool:
        """Under ``TOOL_SEND`` a missing topic is an ordinary refusal: Hermes' prompts and albums, which
        decide by this, then fail instead of retrying in the root chat."""
        from .harvested.adapter_core import CoreMixin

        return not TOOL_SEND.get() and CoreMixin._is_thread_not_found_error(error)

    async def _send_chunk_markdown_or_plain(self, chunk: str, send_kwargs: dict[str, Any]) -> Any:
        """Under ``TOOL_SEND`` a text whose topic is gone fails. The harvested chunk loop reroutes a text whose
        topic Telegram no longer has (``telegram_dm_topic_send.topic_gone``) to the root chat; the refusal it is
        given instead names no topic, so it ends the send as any other refusal does."""
        try:
            return await super()._send_chunk_markdown_or_plain(chunk, send_kwargs)
        except Exception as exc:
            if not (TOOL_SEND.get() and _routed_to_topic(send_kwargs) and self._is_bad_request_error(exc)):
                raise
            from telegram.error import BadRequest

            from .harvested.telegram_dm_topic_send import topic_gone

            if topic_gone(send_kwargs.get("chat_id"), exc):
                raise BadRequest(TOPIC_GONE) from exc
            raise

    async def _send_with_dm_topic_reply_anchor_retry(self, send_fn: Any, send_kwargs: dict[str, Any],
                                                     metadata: Optional[dict[str, Any]], *args: Any,
                                                     **kwargs: Any) -> Any:
        """Under ``TOOL_SEND`` a media send whose private-chat topic is gone fails. The harvested retry
        reroutes it to the chat root unless the send is marked for a named topic
        (``telegram_dm_topic_created_for_send``, read there only by ``dead_topic_may_reroute``); a tool's
        send is marked so. A deleted reply anchor is still dropped: that retry keeps the topic."""
        if TOOL_SEND.get():
            metadata = {**(metadata or {}), "telegram_dm_topic_created_for_send": True}
        return await super()._send_with_dm_topic_reply_anchor_retry(send_fn, send_kwargs, metadata, *args, **kwargs)

    def admitted_turn(self, turn: Turn) -> AdmittedTurn:
        """The record of the human event ``turn`` runs for, or ``TurnRefused``. On the gateway loop."""
        admitted = self._human_turns().get(_event_key(turn.chat_id, turn.inbound_message_id))
        if admitted is None or (admitted.thread_id or None) != (turn.thread_id or None):
            raise TurnRefused(_NOT_A_USER_TURN)
        return admitted


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


async def as_tool_send(make: Callable[[], Awaitable[T]]) -> T:
    """``make()`` with ``TOOL_SEND`` set, in the task that runs it."""
    token = TOOL_SEND.set(True)
    try:
        return await make()
    finally:
        TOOL_SEND.reset(token)


async def on_gateway_loop(runner: Any, make: Callable[[], Awaitable[T]]) -> T:
    """Await ``make()`` on the gateway's loop: adapter state (locks, the bot's HTTP client, the admitted turns)
    belongs to it."""
    loop = getattr(runner, "_gateway_loop", None)
    if loop is None or loop is asyncio.get_running_loop():
        return await make()
    if not loop.is_running():
        raise TurnRefused("The Telegram gateway is not running.")
    future = asyncio.run_coroutine_threadsafe(make(), loop)
    # A cancelled caller must not cancel a send already queued there: a retry would duplicate it.
    return await asyncio.shield(asyncio.wrap_future(future))

