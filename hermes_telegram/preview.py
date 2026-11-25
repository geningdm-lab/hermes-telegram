"""The preview of a turn in a private chat: status, partial answer, Stop (docs/design.md, Turn lifecycle;
slice 3, rows 6 to 10 and 17).

A preview is a Telegram draft (sendRichMessageDraft, core.telegram.org/bots/api/methods/sendrichmessagedraft.md):
a temporary bubble that lives about 30 s, disappears when the bot sends a message, and carries a Stop button
(``can_stop``) whose press arrives as the ``stopped_message_generation`` update. Each frame is the status in
a ``<tg-thinking>`` block, then the partial answer so far.

- ``thinking``: ``on_processing_start`` of the user's own message shows ``Contemplating...`` (row 6); the
  agent's ``telegram_status`` replaces it (``set_preview_status``).
- ``streaming``: Hermes' stream consumer calls ``send_draft`` with the partial answer (row 7). A frame is
  sent again before the draft expires, while the turn runs.
- ``final``: Hermes pauses typing before it delivers a turn's final (``gateway/platforms/base.py:4530``,
  ``stream_consumer._notify_before_finalize``), so a paused chat's preview is not shown again; the final
  message removes the draft (row 8). ``on_processing_complete`` closes the preview.
- Any other message during the turn (a tool's file, a cron report, a notice, a segment the stream sealed)
  removes the draft; it is shown again just after (row 17). An approval or clarify prompt pauses typing
  too, but is never the final, so the preview comes back under it and stays while the user answers.
- ``stopped``: the Stop press dispatches ``/stop`` through the gateway's own command path, as if the user
  had typed it (``_dispatch_active_session_command`` sends Hermes' "Stopped" reply, then cancels the turn);
  the preview stops changing, and the partial answer, if any, is sent as a message after "Stopped" (row 9).
  Hermes' stream consumer drops a draft's partial text on cancel (``stream_consumer._on_cancelled``).
- ``failed``: Hermes' failure reply or error notice is a message, so it removes the draft (row 10).

Groups and channels have no drafts and no preview; they keep Hermes' typing indicator and edit-based
streaming (out of scope, PRD).
"""

import asyncio
import contextlib
import html
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from gateway.platforms.base import SendResult

from . import rich

logger = logging.getLogger(__name__)

DEFAULT_STATUS = "Contemplating..."
STATUS_MAX = 120
REFRESH_SECS = 20.0  # a draft lives about 30 s (sendRichMessageDraft)
RESHOW_DELAY = 1.0  # after a message during the turn; a turn's last message closes the preview first
_KEEPALIVE_TICK = 2.0
_FRAME_CHARS = rich.MAX_CHARS - 1024  # room for the status block
_PLAIN_DRAFT_CHARS = 4096  # sendMessageDraft


@dataclass
class Preview:
    """The preview of one turn: where it is, what it shows, and where it stands."""

    chat_id: str
    thread_id: Optional[str]
    message_id: str  # the user's message the turn answers
    source: Any  # that message's session source: a Stop press acts as this user in this chat
    metadata: dict[str, Any]
    draft_id: int
    status: str = DEFAULT_STATUS
    text: str = ""
    paused: bool = False  # typing paused: the final, or a prompt, is on its way
    shown_while_paused: bool = False  # a prompt was sent while paused: keep the preview under it
    stopped: bool = False
    closed: bool = False
    last_push: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: set[asyncio.Task] = field(default_factory=set)

    @property
    def live(self) -> bool:
        return not (self.closed or self.stopped)

    @property
    def showable(self) -> bool:
        return self.live and (not self.paused or self.shown_while_paused)


def _key(chat_id: Any, thread_id: Any) -> tuple[str, Optional[str]]:
    return str(chat_id), (str(thread_id) if thread_id else None)


def frame_markdown(status: str, text: str) -> str:
    """A frame: the status in a thinking block, then the partial answer prepared as a rich message."""
    head = f"<tg-thinking>{html.escape(status, quote=False)}</tg-thinking>"
    if not text.strip():
        return head
    body = rich.prepare_markdown(text)
    if len(body) > _FRAME_CHARS:
        cut = body.rfind("\n", 0, _FRAME_CHARS)
        body = body[:cut if cut > 0 else _FRAME_CHARS]
    return f"{head}\n\n{body}"


class PreviewMixin:
    """Turn previews in private chats; listed before the harvested mixins."""

    # --- state -------------------------------------------------------------------------------------------

    def _previews(self) -> dict[tuple[str, Optional[str]], Preview]:
        return self.__dict__.setdefault("_hermes_telegram_previews", {})

    def _preview(self, chat_id: Any, thread_id: Any = None) -> Optional[Preview]:
        return self._previews().get(_key(chat_id, thread_id))

    def _previews_of_chat(self, chat_id: Any) -> list[Preview]:
        return [p for p in list(self._previews().values()) if p.chat_id == str(chat_id)]

    def _spawn(self, preview: Preview, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        preview.tasks.add(task)
        task.add_done_callback(preview.tasks.discard)

    @staticmethod
    def _cancel_tasks(preview: Preview) -> None:
        current = asyncio.current_task()
        for task in list(preview.tasks):
            if task is not current:
                task.cancel()

    def _close_preview(self, preview: Preview) -> None:
        preview.closed = True
        self._cancel_tasks(preview)

    # --- turn hooks --------------------------------------------------------------------------------------

    async def on_processing_start(self, event: Any) -> None:
        await super().on_processing_start(event)
        source = getattr(event, "source", None)
        message_id = getattr(event, "message_id", None) or getattr(source, "message_id", None)
        chat_type = str(getattr(source, "chat_type", "") or "").lower()
        if (getattr(event, "internal", False) or source is None or not message_id or chat_type not in {"dm", "private"}
                or not self.supports_draft_streaming(chat_type=chat_type)):
            return
        from gateway.platforms.base import _thread_metadata_for_event

        key = _key(source.chat_id, source.thread_id)
        stale = self._previews().pop(key, None)
        if stale is not None:
            self._close_preview(stale)
        preview = Preview(chat_id=key[0], thread_id=key[1], message_id=str(message_id), source=source,
                          metadata=dict(_thread_metadata_for_event(event) or {}),
                          draft_id=secrets.randbits(48) or 1)
        self._previews()[key] = preview
        self.__dict__["_hermes_telegram_preview_loop"] = asyncio.get_running_loop()
        await self._push_frame(preview)
        self._spawn(preview, self._keep_alive(preview))

    async def on_processing_complete(self, event: Any, outcome: Any) -> None:
        source = getattr(event, "source", None)
        message_id = getattr(event, "message_id", None) or getattr(source, "message_id", None)
        if source is not None:
            key = _key(source.chat_id, source.thread_id)
            preview = self._previews().get(key)
            if preview is not None and preview.message_id == str(message_id):
                self._previews().pop(key, None)
                self._close_preview(preview)
        await super().on_processing_complete(event, outcome)

    # --- frames ------------------------------------------------------------------------------------------

    async def _push_frame(self, preview: Preview) -> bool:
        """Send the preview's current frame; True when Telegram took it. Never raises."""
        async with preview.lock:
            if not preview.showable:
                return False
            markdown = frame_markdown(preview.status, preview.text)
            kwargs = self._thread_kwargs_for_draft(preview.chat_id, preview.metadata)
            ok = await self._send_rich_frame(preview, markdown, kwargs) or await self._send_plain_frame(preview, kwargs)
            if ok:
                preview.last_push = time.monotonic()
            return ok

    async def _send_rich_frame(self, preview: Preview, markdown: str, kwargs: dict[str, Any]) -> bool:
        if not self._bot or getattr(self, "_rich_draft_disabled", False) or not self._bot_supports_rich():
            return False
        from .harvested.adapter_module import normalize_telegram_chat_id

        payload = {"chat_id": normalize_telegram_chat_id(preview.chat_id), "draft_id": preview.draft_id,
                   "rich_message": {"markdown": markdown}, "can_stop": True, **kwargs}
        try:
            return bool(await self._bot.do_api_request("sendRichMessageDraft", api_kwargs=payload))
        except Exception as exc:
            if self._is_rich_capability_error(exc):
                self._rich_draft_disabled = True
            logger.debug("[%s] rich preview frame not sent: %s", self.name, exc)
            return False

    async def _send_plain_frame(self, preview: Preview, kwargs: dict[str, Any]) -> bool:
        """The same frame as a plain draft, when Telegram refuses the rich one (its block count, say)."""
        if not self._bot or not hasattr(self._bot, "send_message_draft"):
            return False
        from .harvested.adapter_module import normalize_telegram_chat_id

        text = f"{preview.status}\n\n{rich.plain_source(rich.prepare_markdown(preview.text))}".strip()
        try:
            # can_stop is Bot API 10.3, newer than python-telegram-bot 22.8: passed as an extra argument.
            return bool(await self._bot.send_message_draft(
                chat_id=normalize_telegram_chat_id(preview.chat_id), draft_id=preview.draft_id,
                text=text[:_PLAIN_DRAFT_CHARS], api_kwargs={"can_stop": True}, **kwargs))
        except Exception as exc:
            logger.debug("[%s] preview frame not sent: %s", self.name, exc)
            return False

    async def _keep_alive(self, preview: Preview) -> None:
        """Send the frame again before Telegram lets the draft expire, while the turn runs."""
        while preview.live:
            await asyncio.sleep(_KEEPALIVE_TICK)
            if preview.showable and time.monotonic() - preview.last_push >= REFRESH_SECS:
                await self._push_frame(preview)

    def _reshow_soon(self, chat_id: Any, *, prompt: bool = False) -> None:
        """Show the previews of ``chat_id`` again after a message removed them (row 17)."""
        for preview in self._previews_of_chat(chat_id):
            if not preview.live:
                continue
            if prompt and preview.paused:
                preview.shown_while_paused = True
            if preview.showable:
                self._spawn(preview, self._reshow_after(preview))

    async def _reshow_after(self, preview: Preview) -> None:
        await asyncio.sleep(RESHOW_DELAY)
        await self._push_frame(preview)

    # --- what Hermes calls -------------------------------------------------------------------------------

    async def send_draft(self, chat_id: str, draft_id: int, content: str,
                         metadata: Optional[dict[str, Any]] = None) -> SendResult:
        """The stream consumer's partial answer, shown in the turn's preview with its status."""
        preview = self._preview(chat_id, self._metadata_thread_id(metadata))
        if preview is None or preview.closed:
            return await super().send_draft(chat_id, draft_id, content, metadata)
        if preview.stopped:
            return SendResult(success=True)  # stopped: the preview no longer changes
        preview.text, preview.draft_id = content, int(draft_id)
        await self._push_frame(preview)
        # A frame Telegram did not take is not the stream's failure: the next frame, or the keep-alive,
        # shows the text. Failing here would switch the turn to edited messages (stream_consumer_transport).
        return SendResult(success=True)

    def pause_typing_for_chat(self, chat_id: str) -> None:
        # Called from the agent's thread too (approvals): flags only.
        super().pause_typing_for_chat(chat_id)
        for preview in self._previews_of_chat(chat_id):
            preview.paused, preview.shown_while_paused = True, False

    def resume_typing_for_chat(self, chat_id: str) -> None:
        super().resume_typing_for_chat(chat_id)
        previews = self._previews_of_chat(chat_id)
        for preview in previews:
            preview.paused = False
        loop = self.__dict__.get("_hermes_telegram_preview_loop")
        if previews and loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._reshow_soon, chat_id)

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        result = await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)
        self._reshow_soon(chat_id)
        return result

    async def _send_prompt(self, what: str, chat_id: str, metadata: Optional[dict[str, Any]], build: Any,
                           **kwargs: Any) -> SendResult:
        result = await super()._send_prompt(what, chat_id, metadata, build, **kwargs)
        self._reshow_soon(chat_id, prompt=True)
        return result

    async def _send_with_dm_topic_reply_anchor_retry(self, send_fn: Any, kwargs: dict[str, Any], *args: Any,
                                                     **more: Any) -> Any:
        """Every native media send passes here (files, albums)."""
        try:
            return await super()._send_with_dm_topic_reply_anchor_retry(send_fn, kwargs, *args, **more)
        finally:
            chat_id = kwargs.get("chat_id")
            if chat_id is not None:
                self._reshow_soon(chat_id)

    # --- what the agent and the user do -----------------------------------------------------------------

    async def set_preview_status(self, chat_id: str, thread_id: Optional[str], status: str) -> bool:
        """``telegram_status``: the preview's status line from now on; True when a preview shows it."""
        preview = self._preview(chat_id, thread_id)
        if preview is None or not preview.live:
            return False
        preview.status = status.strip()[:STATUS_MAX] or DEFAULT_STATUS
        return await self._push_frame(preview)

    def _register_handlers(self, app: Any) -> None:
        super()._register_handlers(app)
        from telegram import Update
        from telegram.ext import TypeHandler

        # Its own group, first: an update PTB does not model reaches every TypeHandler(Update).
        app.add_handler(TypeHandler(Update, self._on_generation_stopped), group=-10)

    async def _on_generation_stopped(self, update: Any, context: Any) -> None:
        """The user pressed Stop on a preview: stop the turn as ``/stop`` would, then keep its partial answer."""
        stopped = (getattr(update, "api_kwargs", None) or {}).get("stopped_message_generation")
        if not isinstance(stopped, dict):
            return
        self._accept_update()
        chat_id = (stopped.get("chat") or {}).get("id")
        thread_id = stopped.get("message_thread_id")
        preview = self._preview(chat_id, thread_id) or next(iter(self._previews_of_chat(chat_id)), None)
        if preview is None or not preview.live:
            logger.info("[%s] Stop pressed on a preview with no turn running (chat=%s)", self.name, chat_id)
            return
        # Stopped, not closed: the turn's stream may still send frames, which must not show (send_draft).
        preview.stopped = True
        self._cancel_tasks(preview)
        from gateway.platforms.base import MessageEvent, MessageType

        try:
            await self.handle_message(MessageEvent(text="/stop", message_type=MessageType.COMMAND,
                                                   source=preview.source))
        except Exception:
            logger.warning("[%s] /stop from the Stop button failed", self.name, exc_info=True)
            return
        partial = preview.text.strip()
        if partial:
            with contextlib.suppress(Exception):
                await self.send(preview.chat_id, partial, metadata=preview.metadata or None)
