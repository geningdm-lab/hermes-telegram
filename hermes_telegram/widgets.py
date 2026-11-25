"""Widgets: the agent's visual answers, and the requests they send back (docs/design.md, Tools, Events;
slice 5, rows 12 to 14).

A widget (core.telegram.org/bots/widgets) is one self-contained HTML document with inline CSS and JavaScript,
drawn inside a message and run on the user's device in a QuickJS sandbox with no network at all. Its code
never changes after it is sent; its state (``chat_data``, 1024 bytes) is the bot's to write. In production,
widgets work only in the private chat with the bot.

- ``check_widget`` holds the HTML to Telegram's limits, counted as the server counts them (at most 32768
  bytes of UTF-8 and 4096 bytes after zlib at level 9; no NUL), and to the sandbox: a network API throws
  and an external resource never loads there, so a widget that relies on one is refused before it is sent.
- Interaction inside the widget stays on the device and reaches nobody (row 13). The widget's one way out
  is ``TelegramWidget.request(data)``, 1-64 bytes, arriving as a ``widget_query`` update. Telegram wants
  every query answered within 15 s, so the adapter answers at once (without data), then passes the request
  to the agent as ``[widget] data="<data>" on widget <message id>`` from the user who tapped (row 14). The
  agent's real answer is a chat message, or new widget state through ``telegram_message``.
- A widget may send requests in the background while it is visible (one every 5 s): the same request from
  the same widget within ``REPEAT_SECS`` reaches the agent once.
"""

import logging
import re
import time
import zlib
from collections import OrderedDict
from typing import Any, Optional

logger = logging.getLogger(__name__)

HTML_MAX = 32768  # bytes of UTF-8 (sendwidget.md)
COMPRESSED_MAX = 4096  # bytes after zlib at level 9
DATA_MAX = 1024  # chat_data, public_data, answer data
REPEAT_SECS = 10.0
_RECENT_MEMORY = 512

# What the sandbox does not provide (bots/widgets, "Offline"): a widget relying on it would not work.
_NETWORK_API = re.compile(r"\b(?:fetch\s*\(|XMLHttpRequest|WebSocket|EventSource|sendBeacon|importScripts)")
_EXTERNAL = re.compile(
    r"<script\b[^>]*\bsrc\s*=|<link\b|<iframe\b|<object\b|<embed\b|@import\b"
    r"|\b(?:src|href|xlink:href|action)\s*=\s*[\"']?\s*(?:https?:)?//|url\(\s*[\"']?\s*(?:https?:)?//",
    re.IGNORECASE)


def check_widget(html: str) -> Optional[str]:
    """Why Telegram or its sandbox would refuse ``html``, or ``None`` when it is a valid widget."""
    if not html.strip():
        return "the widget's HTML is empty"
    if "\x00" in html:
        return "the HTML contains a NUL character, which Telegram refuses"
    size = len(html.encode("utf-8"))
    if size > HTML_MAX:
        return f"the HTML is {size} bytes; a widget has at most {HTML_MAX}"
    packed = len(zlib.compress(html.encode("utf-8"), 9))
    if packed > COMPRESSED_MAX:
        return (f"the HTML is {packed} bytes compressed; a widget has at most {COMPRESSED_MAX}: draw with SVG and "
                "CSS rather than embedded images, and shorten the code")
    found = _NETWORK_API.search(html)
    if found:
        return f"the widget has no network ({found.group(0).strip('(').strip()} throws in its sandbox)"
    found = _EXTERNAL.search(html)
    if found:
        return (f"the widget loads nothing from outside ({found.group(0).strip()[:40]}): inline the CSS and script, "
                "use data: URLs or inline SVG for images")
    return None


def widget_event_text(data: str, message_id: Any) -> str:
    return f'[widget] data="{data}" on widget {message_id}'


class WidgetsMixin:
    """Widgets and their queries; listed before the harvested mixins."""

    async def send_widget(self, chat_id: str, html: str, metadata: Optional[dict[str, Any]], *,
                          caption: str = "", height: Optional[int] = None, width: Optional[int] = None,
                          state: str = "") -> Any:
        from gateway.platforms.base import SendResult

        from .harvested.adapter_module import _redact_telegram_error_text, normalize_telegram_chat_id

        if int(normalize_telegram_chat_id(chat_id)) <= 0:
            return SendResult(success=False, error="widgets work only in the private chat with the bot")
        routing = self._compute_single_send_routing(chat_id, None, metadata, self._metadata_thread_id(metadata))
        if routing is None:
            return SendResult(success=False, error=self._dm_topic_missing_anchor_error())
        reply_to_id, thread_kwargs = routing
        payload: dict[str, Any] = {"chat_id": normalize_telegram_chat_id(chat_id), "html": html,
                                   "allow_queries": True, **{k: v for k, v in thread_kwargs.items() if v is not None}}
        for key, value in (("caption", caption), ("height", height), ("width", width), ("chat_data", state)):
            if value:
                payload[key] = value
        if reply_to_id is not None:
            payload["reply_parameters"] = {"message_id": reply_to_id}
        await self._before_message(chat_id, self._metadata_thread_id(metadata))  # the preview's fence
        try:
            sent = await self._bot.do_api_request("sendWidget", api_kwargs=payload)
        except Exception as exc:
            return SendResult(success=False, error=_redact_telegram_error_text(exc))
        message_id = sent.get("message_id") if isinstance(sent, dict) else getattr(sent, "message_id", None)
        self._reshow_soon(chat_id, self._metadata_thread_id(metadata))
        result = SendResult(success=True, message_id=str(message_id) if message_id is not None else None)
        self._remember_bot_messages(chat_id, [result])
        return result

    async def set_widget_state(self, chat_id: str, message_id: str, state: str) -> Any:
        """The widget's chat state (editMessageWidgetData ``chat_data``); the widget redraws from it."""
        from gateway.platforms.base import SendResult

        from .harvested.adapter_module import _redact_telegram_error_text, normalize_telegram_chat_id

        try:
            await self._bot.do_api_request("editMessageWidgetData", api_kwargs={
                "chat_id": normalize_telegram_chat_id(chat_id), "message_id": int(message_id), "chat_data": state})
        except Exception as exc:
            return SendResult(success=False, error=_redact_telegram_error_text(exc))
        return SendResult(success=True, message_id=str(message_id))

    def _register_handlers(self, app: Any) -> None:
        super()._register_handlers(app)
        from telegram import Update
        from telegram.ext import TypeHandler

        # PTB 22.8 does not model widget_query: it arrives in Update.api_kwargs.
        app.add_handler(TypeHandler(Update, self._on_widget_query), group=-7)

    async def _on_widget_query(self, update: Any, context: Any) -> None:
        query = (getattr(update, "api_kwargs", None) or {}).get("widget_query")
        if not isinstance(query, dict) or not query.get("id"):
            return
        self._accept_update()
        try:  # every query is answered, at once: Telegram allows 15 s
            await self._bot.do_api_request("answerWidgetQuery", api_kwargs={"widget_query_id": str(query["id"])})
        except Exception:
            logger.warning("[%s] widget query %s not answered", self.name, query.get("id"), exc_info=True)
        user, chat, message_id = query.get("from") or {}, query.get("chat") or {}, query.get("message_id")
        data = str(query.get("data") or "")
        if not user.get("id") or not chat.get("id") or message_id is None or not data:
            return  # an inline message's widget: out of scope (production has widgets in private chats only)
        recent = self.__dict__.setdefault("_hermes_telegram_widget_requests", OrderedDict())
        key, now = (str(chat["id"]), str(message_id), str(user["id"]), data), time.monotonic()
        if now - recent.get(key, -REPEAT_SECS) < REPEAT_SECS:
            return  # the widget asked the same again (a background refresh): the agent has it
        recent[key] = now
        while len(recent) > _RECENT_MEMORY:
            recent.popitem(last=False)
        from telegram import User

        await self.handle_message(self._action_event(
            str(chat["id"]), None, User.de_json(user, self._bot), widget_event_text(data, message_id),
            f"wq{update.update_id}", str(message_id)))
