"""The preview of a turn in a private chat: status, partial answer, Stop (docs/design.md, Turn lifecycle;
slice 3, rows 6 to 10 and 17).

A preview is a Telegram draft (sendRichMessageDraft, core.telegram.org/bots/api/methods/sendrichmessagedraft.md):
a temporary bubble that lives about 30 s, disappears when the bot sends a message, and carries a Stop button
(``can_stop``) whose press arrives as the ``stopped_message_generation`` update. Each frame is the status in
a ``<tg-thinking>`` block, then the part of the answer not yet in the chat.

- ``thinking``: ``on_processing_start`` of a message from an admitted user shows ``Contemplating...``
  (row 6); the agent's ``telegram_status`` replaces it (``set_preview_status``).
- ``streaming``: Hermes' stream consumer calls ``send_draft`` with the partial answer (row 7). A frame is
  sent again before the draft expires, while the turn runs.
- ``final``: Hermes pauses typing (one flag per chat) right before it delivers a turn's final
  (``gateway/platforms/base.py:4530``, ``stream_consumer._notify_before_finalize``). The next message to a
  topic whose preview saw that pause is its final: the preview is fenced (a frame in flight lands first) and
  not shown again. A stream that sends another frame after the pause is not finalizing, so the pause does
  not stop other topics' previews.
- Any other message during the turn (a tool's file, a cron report, a notice, a segment the stream sealed)
  removes the draft; it is shown again just after (row 17). An approval or clarify prompt pauses typing
  too, but is never the final, so the preview comes back under it and stays while the user answers. A
  segment the stream sealed is in the chat: the preview and Stop leave it out.
- ``stopped``: the Stop press of a turn's own draft, by an admitted user, dispatches ``/stop`` through the
  gateway's own command path, as if the user had typed it (``_dispatch_active_session_command`` sends
  Hermes' "Stopped" reply, kept rather than deleted after ``display.ephemeral_system_ttl``, then cancels the
  turn); the preview stops changing, and the part of the answer not yet in the chat is sent after "Stopped"
  (row 9). Hermes' stream consumer drops a draft's partial text on cancel (``stream_consumer._on_cancelled``).
- ``failed``: Hermes' failure reply or error notice is a message, so it removes the draft (row 10).

Frames honour flood waits, Telegram Desktop's crash guards (maths in ``<details>``, CJK; the plain frame
instead) and a topic where Telegram refuses drafts (the stream then falls back to edited messages). Groups
and channels have no drafts and no preview; a private chat streamed by editing messages
(``streaming.transport: edit``) gets none either once that is seen.
"""

import asyncio
import contextvars
import html
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Optional

from gateway.platforms.base import SendResult

from . import rich

logger = logging.getLogger(__name__)

DEFAULT_STATUS = "Contemplating..."
STATUS_MAX = 120
DRAFT_LIFETIME = 30.0  # sendRichMessageDraft: "a temporary 30-second preview"
REFRESH_SECS = 20.0
RESHOW_DELAY = 1.0  # after a message during the turn
_KEEPALIVE_TICK = 2.0
_FRAME_CHARS = rich.MAX_CHARS - 1024  # room for the status block
_PLAIN_DRAFT_CHARS = 4096  # sendMessageDraft

# Set while the Stop button's /stop is dispatched: its reply is kept (row 9 wants it persisted).
_STOP_DISPATCH: contextvars.ContextVar[bool] = contextvars.ContextVar("hermes_telegram_stop_dispatch",
                                                                     default=False)

OK, TRANSIENT, REFUSED, FLOOD = "ok", "transient", "refused", "flood"


@dataclass
class Preview:
    """The preview of one turn: where it is, what it shows, and where it stands."""

    chat_id: str
    thread_id: Optional[str]
    message_id: str  # the user's message the turn answers
    source: Any  # that message's session source: a Stop press acts as this user in this chat
    metadata: dict[str, Any]
    draft_id: int
    draft_ids: set[int] = field(default_factory=set)  # every draft this turn showed
    status: str = DEFAULT_STATUS
    text: str = ""  # the stream's current segment, as the agent wrote it
    sealed: list[str] = field(default_factory=list)  # prepared text of the segment already in the chat
    pause_pending: bool = False  # Hermes paused typing in this chat: the next message here may be the final
    paused: bool = False  # this turn's final is being delivered
    shown_while_paused: bool = False  # a prompt was sent while paused: keep the preview under it
    stopped: bool = False
    closed: bool = False
    unusable: bool = False  # Telegram refuses drafts here, or the stream edits messages instead
    cooldown_until: float = 0.0
    last_push: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: set[asyncio.Task] = field(default_factory=set)

    @property
    def live(self) -> bool:
        return not (self.closed or self.stopped or self.unusable)

    @property
    def showable(self) -> bool:
        return self.live and (not self.paused or self.shown_while_paused) and time.monotonic() >= self.cooldown_until

    def unsent_markdown(self) -> str:
        """The prepared segment without the part of it already sealed into the chat."""
        body = rich.prepare_markdown(self.text).strip()
        for sealed in self.sealed:
            if sealed and body.startswith(sealed):
                body = body[len(sealed):].strip()
        return body


def _key(chat_id: Any, thread_id: Any) -> tuple[str, Optional[str]]:
    return str(chat_id), (str(thread_id) if thread_id else None)


def frame_markdown(status: str, body: str) -> str:
    """A frame: the status in a thinking block, then ``body`` (prepared Markdown) when there is one."""
    head = f"<tg-thinking>{html.escape(status, quote=False)}</tg-thinking>"
    if not body.strip():
        return head
    if len(body) > _FRAME_CHARS:
        cut = body.rfind("\n", 0, _FRAME_CHARS)
        body = body[:cut if cut > 0 else _FRAME_CHARS]
    return f"{head}\n\n{body}"


def _retry_after(exc: BaseException) -> Optional[float]:
    wait = getattr(exc, "retry_after", None)
    if isinstance(wait, timedelta):
        return wait.total_seconds()
    return float(wait) if wait is not None else None


class PreviewMixin:
    """Turn previews in private chats; listed before the harvested mixins."""

    # --- state -------------------------------------------------------------------------------------------

    def _previews(self) -> dict[tuple[str, Optional[str]], Preview]:
        return self.__dict__.setdefault("_hermes_telegram_previews", {})

    def _preview(self, chat_id: Any, thread_id: Any = None) -> Optional[Preview]:
        return self._previews().get(_key(chat_id, thread_id))

    def _previews_of_chat(self, chat_id: Any) -> list[Preview]:
        return [p for p in list(self._previews().values()) if p.chat_id == str(chat_id)]

    def _preview_for_send(self, chat_id: Any, thread_id: Any) -> Optional[Preview]:
        """The preview a message to ``chat_id`` (and ``thread_id``, when known) lands beside."""
        found = self._preview(chat_id, thread_id)
        if found is None and not thread_id:
            previews = self._previews_of_chat(chat_id)
            found = previews[0] if len(previews) == 1 else None
        return found

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

    def _admitted_user(self, chat_id: Any, thread_id: Any, user_id: Any, user_name: Any = None) -> bool:
        """The adapter's admission check for buttons (the gateway's full chain when a runner is present)."""
        return bool(user_id) and self._is_callback_user_authorized(
            str(user_id), chat_id=str(chat_id), chat_type="private",
            thread_id=str(thread_id) if thread_id else None, user_name=user_name)

    # --- turn hooks --------------------------------------------------------------------------------------

    async def on_processing_start(self, event: Any) -> None:
        await super().on_processing_start(event)
        source = getattr(event, "source", None)
        message_id = getattr(event, "message_id", None) or getattr(source, "message_id", None)
        chat_type = str(getattr(source, "chat_type", "") or "").lower()
        if (getattr(event, "internal", False) or source is None or not message_id or chat_type not in {"dm", "private"}
                or not self.supports_draft_streaming(chat_type=chat_type)
                or not self._admitted_user(source.chat_id, source.thread_id, source.user_id, source.user_name)):
            return
        from gateway.platforms.base import _thread_metadata_for_event

        key = _key(source.chat_id, source.thread_id)
        stale = self._previews().pop(key, None)
        if stale is not None:
            self._close_preview(stale)
        draft_id = secrets.randbits(48) or 1
        preview = Preview(chat_id=key[0], thread_id=key[1], message_id=str(message_id), source=source,
                          metadata=dict(_thread_metadata_for_event(event) or {}), draft_id=draft_id,
                          draft_ids={draft_id})
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

    async def _push_frame(self, preview: Preview) -> str:
        """Send the preview's current frame. Never raises; the outcome is ``OK``, ``TRANSIENT`` (try the next
        frame), ``REFUSED`` (Telegram takes no draft here) or ``FLOOD`` (waiting out a flood penalty)."""
        async with preview.lock:
            if not preview.showable:
                return TRANSIENT
            body = preview.unsent_markdown()
            kwargs = self._thread_kwargs_for_draft(preview.chat_id, preview.metadata)
            outcome = REFUSED if self._rich_unsafe_here(body) else await self._send_rich_frame(preview, body, kwargs)
            if outcome in (REFUSED, TRANSIENT):
                # The plain frame is short text without markup: Telegram refusing that refuses drafts here
                # (private topics may), not this frame's content.
                outcome = await self._send_plain_frame(preview, body, kwargs) if preview.showable else TRANSIENT
            if outcome == OK:
                preview.last_push = time.monotonic()
            elif outcome == REFUSED:
                preview.unusable = True
                self._cancel_tasks(preview)
            return outcome

    def _rich_unsafe_here(self, body: str) -> bool:
        """The Telegram Desktop guards the final answer keeps (tdesktop#30808, Hermes #47653)."""
        return bool(not self._bot_supports_rich() or getattr(self, "_rich_draft_disabled", False)
                    or self._has_telegram_desktop_details_math_crash_shape(body)
                    or (not getattr(self, "_allow_cjk_rich_messages", False)
                        and self._has_telegram_desktop_cjk_rich_garble_shape(body)))

    def _frame_failed(self, preview: Preview, exc: Exception, what: str) -> str:
        wait = _retry_after(exc)
        if wait is not None:
            preview.cooldown_until = time.monotonic() + wait
            logger.info("[%s] preview frames wait %.0f s (flood control)", self.name, wait)
            return FLOOD
        logger.debug("[%s] %s preview frame not sent: %s", self.name, what, exc)
        return REFUSED if self._is_bad_request_error(exc) else TRANSIENT

    async def _send_rich_frame(self, preview: Preview, body: str, kwargs: dict[str, Any]) -> str:
        from .harvested.adapter_module import normalize_telegram_chat_id

        payload = {"chat_id": normalize_telegram_chat_id(preview.chat_id), "draft_id": preview.draft_id,
                   "rich_message": {"markdown": frame_markdown(preview.status, body)}, "can_stop": True, **kwargs}
        try:
            return OK if await self._bot.do_api_request("sendRichMessageDraft", api_kwargs=payload) else TRANSIENT
        except Exception as exc:
            if self._is_rich_capability_error(exc):
                self._rich_draft_disabled = True
                return TRANSIENT  # the plain draft follows
            return self._frame_failed(preview, exc, "rich")

    async def _send_plain_frame(self, preview: Preview, body: str, kwargs: dict[str, Any]) -> str:
        """The same frame as a plain draft: for Telegram Desktop's guarded shapes, or when the rich one fails."""
        if not self._bot or not hasattr(self._bot, "send_message_draft"):
            return REFUSED
        from .harvested.adapter_module import normalize_telegram_chat_id

        text = f"{preview.status}\n\n{rich.plain_source(body)}".strip()
        try:
            # can_stop is Bot API 10.3, newer than python-telegram-bot 22.8: passed as an extra argument.
            sent = await self._bot.send_message_draft(
                chat_id=normalize_telegram_chat_id(preview.chat_id), draft_id=preview.draft_id,
                text=text[:_PLAIN_DRAFT_CHARS], api_kwargs={"can_stop": True}, **kwargs)
            return OK if sent else TRANSIENT
        except Exception as exc:
            return self._frame_failed(preview, exc, "plain")

    async def _keep_alive(self, preview: Preview) -> None:
        """Send the frame again before Telegram lets the draft expire, while the turn runs."""
        while preview.live:
            await asyncio.sleep(_KEEPALIVE_TICK)
            if preview.showable and time.monotonic() - preview.last_push >= REFRESH_SECS:
                await self._push_frame(preview)

    def _reshow_soon(self, chat_id: Any, thread_id: Any = None, *, prompt: bool = False) -> None:
        """Show the preview again after a message removed it (row 17)."""
        targets = ([self._preview_for_send(chat_id, thread_id)] if thread_id or prompt
                   else self._previews_of_chat(chat_id))
        for preview in targets:
            if preview is None or not preview.live:
                continue
            if prompt and (preview.paused or preview.pause_pending):
                preview.shown_while_paused = True
            if preview.showable:
                self._spawn(preview, self._reshow_after(preview))

    async def _reshow_after(self, preview: Preview) -> None:
        await asyncio.sleep(RESHOW_DELAY)
        await self._push_frame(preview)

    async def _before_message(self, chat_id: Any, thread_id: Any, content: str = "") -> Optional[Preview]:
        """Fence the preview a message lands beside: a turn's final ends it, after any frame in flight; a
        segment the stream sealed leaves the preview's text."""
        preview = self._preview_for_send(chat_id, thread_id)
        if preview is None or not preview.live:
            return None
        if preview.pause_pending:
            preview.pause_pending, preview.paused, preview.shown_while_paused = False, True, False
            async with preview.lock:  # a frame already on its way lands before the final
                pass
        if content.strip():
            sent = rich.prepare_markdown(content).strip()
            if sent and rich.prepare_markdown(preview.text).strip().startswith(sent):
                preview.sealed.append(sent)
        return preview

    # --- what Hermes calls -------------------------------------------------------------------------------

    async def send_draft(self, chat_id: str, draft_id: int, content: str,
                         metadata: Optional[dict[str, Any]] = None) -> SendResult:
        """The stream consumer's partial answer, shown in the turn's preview with its status."""
        preview = self._preview(chat_id, self._metadata_thread_id(metadata))
        if preview is None or preview.closed:
            return await super().send_draft(chat_id, draft_id, content, metadata)
        if preview.unusable:
            return SendResult(success=False, error="drafts_unavailable")  # the stream edits messages instead
        if preview.stopped:
            return SendResult(success=True)  # stopped: the preview no longer changes
        # A stream still streaming after Hermes paused typing is not the one finalizing.
        preview.pause_pending = preview.paused = False
        if content != preview.text:
            preview.sealed = [s for s in preview.sealed if rich.prepare_markdown(content).strip().startswith(s)]
        preview.text, preview.draft_id = content, int(draft_id)
        preview.draft_ids.add(int(draft_id))
        outcome = await self._push_frame(preview)
        if outcome == REFUSED:
            return SendResult(success=False, error="drafts_unavailable")
        # A frame Telegram did not take this time is shown by the next one, or by the keep-alive.
        return SendResult(success=True)

    def pause_typing_for_chat(self, chat_id: str) -> None:
        # Called from the agent's thread too (approvals): flags only.
        super().pause_typing_for_chat(chat_id)
        for preview in self._previews_of_chat(chat_id):
            preview.pause_pending, preview.shown_while_paused = True, False

    def resume_typing_for_chat(self, chat_id: str) -> None:
        super().resume_typing_for_chat(chat_id)
        previews = self._previews_of_chat(chat_id)
        for preview in previews:
            preview.pause_pending = preview.paused = False
        loop = self.__dict__.get("_hermes_telegram_preview_loop")
        if previews and loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._reshow_soon, chat_id)

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        thread_id = self._metadata_thread_id(metadata)
        preview = await self._before_message(chat_id, thread_id, content)
        if (preview is not None and (metadata or {}).get("expect_edits")
                and not self._is_sealed_head(content)):
            # A message the stream will edit: this chat streams by editing (streaming.transport: edit).
            preview.unusable = True
            self._cancel_tasks(preview)
        result = await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)
        self._reshow_soon(chat_id, thread_id)
        return result

    async def _send_prompt(self, what: str, chat_id: str, metadata: Optional[dict[str, Any]], build: Any,
                           **kwargs: Any) -> SendResult:
        result = await super()._send_prompt(what, chat_id, metadata, build, **kwargs)
        self._reshow_soon(chat_id, kwargs.get("thread_id") or self._metadata_thread_id(metadata), prompt=True)
        return result

    async def _send_with_dm_topic_reply_anchor_retry(self, send_fn: Any, kwargs: dict[str, Any], *args: Any,
                                                     **more: Any) -> Any:
        """Every native media send passes here (files, albums)."""
        chat_id = kwargs.get("chat_id")
        thread_id = kwargs.get("message_thread_id") or kwargs.get("direct_messages_topic_id")
        if chat_id is not None:
            await self._before_message(chat_id, thread_id)
        try:
            return await super()._send_with_dm_topic_reply_anchor_retry(send_fn, kwargs, *args, **more)
        finally:
            if chat_id is not None:
                self._reshow_soon(chat_id, thread_id)

    def _schedule_ephemeral_delete(self, chat_id: str, message_id: str, ttl_seconds: int) -> None:
        if _STOP_DISPATCH.get():
            return  # the Stop button's "Stopped" stays (row 9)
        super()._schedule_ephemeral_delete(chat_id, message_id, ttl_seconds)

    # --- what the agent and the user do -----------------------------------------------------------------

    async def set_preview_status(self, chat_id: str, thread_id: Optional[str], status: str) -> bool:
        """``telegram_status``: the preview's status line from now on; True when a preview shows it."""
        preview = self._preview(chat_id, thread_id)
        if preview is None or not preview.live:
            return False
        preview.status = status.strip()[:STATUS_MAX] or DEFAULT_STATUS
        return await self._push_frame(preview) == OK

    def _register_handlers(self, app: Any) -> None:
        super()._register_handlers(app)
        from telegram import Update
        from telegram.ext import TypeHandler

        # Its own group, first: an update PTB does not model reaches every TypeHandler(Update).
        app.add_handler(TypeHandler(Update, self._on_generation_stopped), group=-10)

    async def _on_generation_stopped(self, update: Any, context: Any) -> None:
        """The user pressed Stop on a preview: stop its turn as ``/stop`` would, then keep its partial answer."""
        stopped = (getattr(update, "api_kwargs", None) or {}).get("stopped_message_generation")
        if not isinstance(stopped, dict):
            return
        self._accept_update()
        chat_id = (stopped.get("chat") or {}).get("id")
        thread_id = stopped.get("message_thread_id")
        try:
            draft_id = int(stopped.get("draft_id"))
        except (TypeError, ValueError):
            return
        preview = self._preview(chat_id, thread_id)
        if preview is None or not preview.live or draft_id not in preview.draft_ids:
            logger.info("[%s] Stop pressed on a draft of no running turn (chat=%s)", self.name, chat_id)
            return
        # A private chat's id is its user's: the press is that user's /stop, admitted as one now.
        if not self._admitted_user(chat_id, thread_id, chat_id, getattr(preview.source, "user_name", None)):
            logger.info("[%s] Stop pressed by a user no longer admitted (chat=%s)", self.name, chat_id)
            return
        # Stopped, not closed: the turn's stream may still send frames, which must not show (send_draft).
        preview.stopped = True
        self._cancel_tasks(preview)
        partial = preview.unsent_markdown()
        from gateway.platforms.base import MessageEvent, MessageType

        token = _STOP_DISPATCH.set(True)
        try:
            await self.handle_message(MessageEvent(text="/stop", message_type=MessageType.COMMAND,
                                                   source=preview.source))
        except Exception:
            logger.warning("[%s] /stop from the Stop button failed", self.name, exc_info=True)
            return
        finally:
            _STOP_DISPATCH.reset(token)
        if partial:
            # The only copy left: Hermes' own retry (a certainly unsent send again, an ambiguous one not).
            result = await self._send_with_retry(preview.chat_id, partial, metadata=preview.metadata or None)
            if not result.success:
                logger.warning("[%s] the partial answer after Stop was not delivered: %s", self.name, result.error)
